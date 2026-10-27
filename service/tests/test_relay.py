import base64
import json
import socket
import threading

import pytest

from app import events
from app.relay import Admission, MAX_BODY, MAX_REQUEST, RelayLogger, create_app


TOKEN = "r" * 32
CORRELATION = "a" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}


def envelope(operation="verification"):
    payload = {"type": "verification", "challenge": "test-challenge"} if operation == "verification" else {
        "eventId": "test-event", "name": "whatsapp.message.received", "timestamp": "2026-10-27T00:00:00Z",
        "data": {"account_id": "fixture", "id": "message"}, "cursor": None}
    body = json.dumps(payload).encode()
    return {"url": "https://callback.test/events?private=fixture", "body": base64.b64encode(body).decode(),
            "headers": {"Content-Type": "application/json", "webhook-id": "fixture-event",
                        "webhook-timestamp": "123", "webhook-signature": "v1,fixture-signature",
                        "X-MCP-Subscription-Id": "fixture-subscription"},
            "operation": operation, "correlation_id": CORRELATION}


@pytest.fixture
def relay():
    return create_app({"WEBHOOK_RELAY_TOKEN": TOKEN, "TESTING": True})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Unexpected network")
    monkeypatch.setattr(events, "_resolve", denied)
    monkeypatch.setattr(events, "_pinned_post", denied)


def post(relay, value=None, headers=None):
    return relay.test_client().post("/internal/webhooks/send", json=value or envelope(), headers=AUTH if headers is None else headers)


def test_minimal_startup_health_and_auth(relay, monkeypatch):
    monkeypatch.setattr("app.models.database", lambda *args: pytest.fail("Relay opened database"))
    assert relay.test_client().get("/healthz").data == b"ok"
    for headers in ({}, {"Authorization": "Bearer " + "i" * 32}):
        result = post(relay, headers=headers)
        assert result.status_code == 401
        assert result.json["correlation_id"] != CORRELATION
        assert result.json["error"] == "connection_failed"
    with pytest.raises(ValueError):
        create_app({"WEBHOOK_RELAY_TOKEN": ""})


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(extra="rejected"),
    lambda value: value.update(correlation_id="private-query-secret"),
    lambda value: value.update(correlation_id="A" * 32),
    lambda value: value.update(url="https://callback.test/\r\ninjected"),
    lambda value: value.update(url="https://callback.test/" + "x" * 2048),
    lambda value: value.update(operation="GET"),
    lambda value: value.update(body=base64.b64encode(b"x" * (MAX_BODY + 1)).decode()),
    lambda value: value.update(body="not-base64!!"),
    lambda value: value.update(body=base64.b64encode(b'{"type":"verification","challenge":NaN}').decode()),
    lambda value: value.update(body=base64.b64encode(b'{"type":"verification","challenge":"a","challenge":"b"}').decode()),
    lambda value: value.update(body=base64.b64encode(b'{}').decode()),
    lambda value: value["headers"].update(Authorization="Bearer secret"),
    lambda value: value["headers"].update({"webhook-id": "secret\nheader"}),
    lambda value: value["headers"].update({"webhook-signature": "x" * 1025}),
    lambda value: value["headers"].update({"Content-Type": "text/plain"}),
    lambda value: value["headers"].pop("webhook-id"),
])
def test_strict_envelope_rejected(relay, mutation):
    value = envelope()
    mutation(value)
    result = post(relay, value)
    assert result.status_code == 400
    assert result.json["error"] == "invalid_destination"


def test_outer_body_bounded_and_generic_errors(relay):
    client = relay.test_client()
    result = client.post("/internal/webhooks/send", data=b"x" * (MAX_REQUEST + 1), headers=AUTH, content_type="application/json")
    assert result.status_code == 400
    for method in ("get", "put", "delete"):
        result = getattr(client, method)("/internal/webhooks/send?secret=never-log")
        assert result.status_code == 405
        assert "secret" not in result.get_data(as_text=True)
    value = json.dumps(envelope())
    result = client.post("/internal/webhooks/send", data=value[:-1] + ',"url":"https://different.test"}', headers=AUTH, content_type="application/json")
    assert result.status_code == 400


@pytest.mark.parametrize("operation", ["verification", "delivery"])
def test_public_resolution_and_pin_each_attempt(relay, monkeypatch, operation):
    ips, received = iter(["8.8.8.8", "1.1.1.1"]), []
    monkeypatch.setattr(events, "public_destination", lambda url: next(ips))
    def pinned(url, ip, body, headers):
        received.append((url, ip, json.loads(body), headers))
        return 302, b"redirect-not-followed"
    monkeypatch.setattr(events, "pinned_post", pinned)
    for expected in ("8.8.8.8", "1.1.1.1"):
        result = post(relay, envelope(operation))
        assert result.status_code == 200
        assert result.json == {"status": 302, "body": base64.b64encode(b"redirect-not-followed").decode(),
                               "destination_ip": expected, "correlation_id": CORRELATION}
    assert [item[1] for item in received] == ["8.8.8.8", "1.1.1.1"]


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["::1"]])
def test_private_and_mixed_dns_rejected_at_relay(relay, monkeypatch, addresses):
    original = events.public_destination
    resolver = lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
    monkeypatch.setattr(events, "public_destination", lambda url: original(url, resolver=resolver))
    monkeypatch.setattr(events, "pinned_post", lambda *args: pytest.fail("Private destination reached transport"))
    assert post(relay).json["error"] == "invalid_destination"


@pytest.mark.parametrize("error,reason,status", [(TimeoutError("private-detail"), "timeout", 503),
                                                  (OSError("private-detail"), "connection_failed", 503),
                                                  (ValueError("private-detail"), "invalid_destination", 400),
                                                  (RuntimeError("private-detail"), "connection_failed", 503)])
def test_transport_failures_sanitized(relay, monkeypatch, error, reason, status):
    def fail(url):
        raise error
    monkeypatch.setattr(events, "public_destination", fail)
    result = post(relay)
    assert result.status_code == status
    assert result.json == {"error": reason, "correlation_id": CORRELATION}
    assert relay.extensions["relay_admission"].busy is False


def test_rate_cap_and_concurrency_no_queue(relay, monkeypatch):
    clock = [0]
    admission = Admission(2, clock=lambda: clock[0])
    assert admission.acquire()
    assert not admission.acquire()
    admission.release()
    assert admission.acquire()
    admission.release()
    assert not admission.acquire()
    clock[0] = 60
    assert admission.acquire()
    assert len(admission.attempts) == 1
    entered, finish, results = threading.Event(), threading.Event(), []
    monkeypatch.setattr(events, "public_destination", lambda url: "8.8.8.8")
    def held(*args):
        entered.set()
        assert finish.wait(5)
        return 200, b"ok"
    monkeypatch.setattr(events, "pinned_post", held)
    worker = threading.Thread(target=lambda: results.append(post(relay).status_code))
    worker.start()
    try:
        assert entered.wait(5)
        assert post(relay).status_code == 429
    finally:
        finish.set()
        worker.join(5)
    assert results == [200]


def test_response_bounds_and_sanitized_logs(relay, monkeypatch, caplog):
    caplog.set_level("INFO", logger="whatsapp.audit")
    monkeypatch.setattr(events, "public_destination", lambda url: "8.8.8.8")
    monkeypatch.setattr(events, "pinned_post", lambda *args: (200, b"x" * 4097))
    assert post(relay).status_code == 200
    monkeypatch.setattr(events, "pinned_post", lambda *args: (200, b"x" * 4098))
    assert post(relay).status_code == 503
    assert caplog.records
    for forbidden in ("callback.test", "fixture", "8.8.8.8", TOKEN, "private="):
        assert forbidden not in caplog.text
    assert CORRELATION in caplog.text


def test_gunicorn_parser_exception_and_access_logs_sanitized(caplog):
    caplog.set_level("INFO", logger="whatsapp.audit")
    logger = object.__new__(RelayLogger)
    for name in ("warning", "error", "exception", "critical", "info", "debug", "access"):
        getattr(logger, name)("raw-secret-url-and-headers", "private-ip", exc_info=True)
    assert len(caplog.records) == 4
    assert "raw-secret" not in caplog.text
    assert "private-ip" not in caplog.text
    assert all(json.loads(record.message)["reason"] == "internal_error" for record in caplog.records)


def test_entrypoint_worker_thread_and_logging_configuration(monkeypatch):
    from app import relay
    from gunicorn.app.base import BaseApplication
    seen = {}
    def run(server):
        seen.update({name: getattr(server.cfg, name) for name in
                     ("bind", "workers", "threads", "timeout", "accesslog", "logger_class")})
    monkeypatch.setattr(BaseApplication, "run", run)
    relay.main()
    assert seen == {"bind": ["0.0.0.0:8001"], "workers": 1, "threads": 4, "timeout": 30,
                    "accesslog": None, "logger_class": RelayLogger}
