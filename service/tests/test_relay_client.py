import base64
import json

import pytest

from app import events, relay_client
from app.events import CallbackEndpointError
from test_relay import CORRELATION, TOKEN, envelope


CONFIG = {"WEBHOOK_RELAY_URL": "http://whatsapp-mcp-webhook-relay:8001", "WEBHOOK_RELAY_TOKEN": TOKEN}


def invoke(config=None):
    value = envelope()
    return relay_client.send_callback(CONFIG if config is None else config, value["url"], base64.b64decode(value["body"]),
                                      value["headers"], operation=value["operation"], correlation_id=value["correlation_id"])


def success(**updates):
    value = {"status": 200, "body": base64.b64encode(b"answer").decode(), "destination_ip": "8.8.8.8", "correlation_id": CORRELATION}
    value.update(updates)
    return json.dumps(value).encode()


def test_client_bounded_internal_hop_only_no_direct_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(events, "pinned_post", lambda *args: pytest.fail("Direct fallback"))
    monkeypatch.setattr(events, "public_destination", lambda *args: pytest.fail("API performed destination DNS"))
    def bounded(operation, args, timeout):
        calls.append((operation, args, timeout))
        return 200, success()
    monkeypatch.setattr(events, "_bounded_call", bounded)
    assert invoke() == (200, b"answer", "8.8.8.8")
    operation, args, timeout = calls[0]
    assert operation is relay_client._relay_post
    assert timeout == 22
    assert args[:2] == (CONFIG["WEBHOOK_RELAY_URL"], TOKEN)
    assert json.loads(args[2])["correlation_id"] == CORRELATION
    for config in ({}, {**CONFIG, "WEBHOOK_RELAY_TOKEN": ""}, {**CONFIG, "WEBHOOK_RELAY_URL": "https://user:password@relay.test"},
                   {**CONFIG, "WEBHOOK_RELAY_URL": "http://relay.test:bad"}, {**CONFIG, "WEBHOOK_RELAY_URL": "http://relay.test/path"}):
        with pytest.raises(CallbackEndpointError):
            invoke(config)
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"x" * 8193,
    success(correlation_id="b" * 32), success(status=True), success(status=600), success(destination_ip="127.0.0.1"),
    success(destination_ip="invalid"), success(body="invalid-base64!!"), success(body=base64.b64encode(b"x" * 4098).decode()),
    success(extra="unexpected"), success()[:-1] + b',"status":201}'])
def test_client_strict_response_schema(monkeypatch, raw):
    monkeypatch.setattr(events, "_bounded_call", lambda *args: (200, raw))
    with pytest.raises(CallbackEndpointError) as error:
        invoke()
    assert error.value.reason == "connection_failed"


@pytest.mark.parametrize("status,reason,expected", [(503, "timeout", "timeout"), (503, "connection_failed", "connection_failed"),
                                                  (429, "capacity", "connection_failed"), (302, "timeout", "connection_failed")])
def test_client_error_conversion_no_fallback(monkeypatch, status, reason, expected):
    monkeypatch.setattr(events, "_bounded_call", lambda *args: (status, json.dumps({"error": reason, "correlation_id": CORRELATION}).encode()))
    with pytest.raises(CallbackEndpointError) as error:
        invoke()
    assert error.value.reason == expected


def test_client_destination_rejection_and_deadline_sanitized(monkeypatch):
    monkeypatch.setattr(events, "_bounded_call", lambda *args: (400, json.dumps({"error": "invalid_destination", "correlation_id": CORRELATION}).encode()))
    with pytest.raises(ValueError, match="^Invalid callback destination$"):
        invoke()
    for thrown, expected in ((TimeoutError("secret"), "timeout"), (OSError("secret"), "connection_failed")):
        def fail(*args):
            raise thrown
        monkeypatch.setattr(events, "_bounded_call", fail)
        with pytest.raises(CallbackEndpointError) as error:
            invoke()
        assert error.value.reason == expected
        assert "secret" not in str(error.value)


def test_internal_transport_disables_redirects_retries_and_proxy_env(monkeypatch):
    seen = {}
    class Response:
        status = 302
        def read(self, size):
            seen["size"] = size
            return b"redirect"
        def close(self):
            seen["closed"] = True
    class Pool:
        def __init__(self, **kwargs):
            seen["pool"] = kwargs
        def request(self, method, url, **kwargs):
            seen.update(method=method, url=url, request=kwargs)
            return Response()
        def clear(self):
            seen["cleared"] = True
    monkeypatch.setenv("HTTPS_PROXY", "http://never-used.test")
    monkeypatch.setattr(relay_client.urllib3, "PoolManager", Pool)
    assert relay_client._relay_post(CONFIG["WEBHOOK_RELAY_URL"], TOKEN, b"request") == (302, b"redirect")
    assert seen["url"] == CONFIG["WEBHOOK_RELAY_URL"] + "/internal/webhooks/send"
    assert seen["request"]["redirect"] is False
    assert seen["request"]["retries"] is False
    assert seen["request"]["preload_content"] is False
    assert seen["request"]["headers"] == {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
    assert seen["size"] == 8193
    assert seen["closed"] and seen["cleared"]
