import base64
import json
from contextlib import contextmanager

import pytest
from flask import g
from sqlalchemy import select

from app.auth import digest
from app.events import CallbackEndpointError, deliver, emit, subscribe
from app.models import Delivery, Subscription, Token, now
from app.observability import valid_correlation_id
from test_api import rpc
from test_auth import authorize, exchange


def params():
    return {"name": "whatsapp.message.received", "arguments": {"account_id": "personal"},
            "delivery": {"mode": "webhook", "url": "https://callback.test/events?private=hidden-marker",
                         "secret": "whsec_"+base64.b64encode(b"s"*32).decode()}}


def token():
    return Token(client_id="desktop", owner_id="owner", access_hash=digest("access"),
                 refresh_hash=digest("refresh"), family="family", resource="https://archive.test/mcp",
                 scope="whatsapp:read", expires_at=now()+900, refresh_expires_at=now()+10000)


def enable(application):
    application.config.update(WEBHOOK_RELAY_URL="http://webhook-relay:8001", WEBHOOK_RELAY_TOKEN="r"*32)


def forbid(*args, **kwargs):
    pytest.fail("API/controller must not use direct callback DNS or HTTPS in relay mode")


def test_verification_and_delivery_use_relay_with_fresh_attempt_identity(application, monkeypatch, caplog):
    enable(application)
    caplog.set_level("INFO", logger="whatsapp.audit")
    calls = []
    def send(config, url, body, headers, operation, correlation_id):
        assert config["WEBHOOK_RELAY_TOKEN"] == "r"*32
        calls.append((operation, correlation_id, headers))
        if operation == "verification":
            assert correlation_id == "a"*32
            return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode(), "8.8.8.8"
        return 204, b"", "9.9.9.9"
    monkeypatch.setattr("app.relay_client.send_callback", send)
    monkeypatch.setattr("app.events.public_destination", forbid)
    monkeypatch.setattr("app.events.pinned_post", forbid)
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        g.correlation_id = "a"*32
        t = token(); g.db.add(t); g.db.flush()
        result = subscribe(params(), t)
        assert g.db.get(Subscription, result["id"]).destination_ip == "8.8.8.8"
        emit(g.db, "personal", "whatsapp.message.received", {"account_id": "personal", "id": "fixture"})
        g.db.commit()
        assert deliver(application.extensions["db_session"], application.config["TOKEN_ENCRYPTION_KEY"],
                       relay_config=application.config)
        g.db.expire_all()
        assert g.db.scalar(select(Delivery)).delivered
        g.db.close()
    assert [call[0] for call in calls] == ["verification", "delivery"]
    assert valid_correlation_id(calls[1][1]) and calls[1][1] != calls[0][1]
    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "whatsapp.audit"]
    assert any(r["component"] == "api" and r["correlation_id"] == calls[0][1] for r in records)
    assert any(r["component"] == "controller" and r["correlation_id"] == calls[1][1] for r in records)
    assert "hidden-marker" not in caplog.text and params()["delivery"]["secret"] not in caplog.text


def test_relay_failure_never_falls_back_or_persists_subscription(application, monkeypatch):
    enable(application)
    def failed(*args, **kwargs): raise CallbackEndpointError("timeout")
    monkeypatch.setattr("app.relay_client.send_callback", failed)
    monkeypatch.setattr("app.events.public_destination", forbid)
    monkeypatch.setattr("app.events.pinned_post", forbid)
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        with pytest.raises(CallbackEndpointError, match="CallbackEndpointError"):
            subscribe(params(), token())
        assert list(g.db.scalars(select(Subscription))) == []
        g.db.close()


def test_failed_delivery_remains_durable_for_retry(application, monkeypatch):
    enable(application)
    monkeypatch.setattr("app.events.public_destination", lambda url: "8.8.8.8")
    def verify(url, ip, body, headers):
        return 200, json.dumps({"challenge": json.loads(body)["challenge"]}).encode()
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        t = token(); g.db.add(t); g.db.flush()
        subscribe(params(), t, post=verify)
        emit(g.db, "personal", "whatsapp.message.received", {"account_id": "personal", "id": "fixture"})
        g.db.commit(); g.db.close()
    def failed(*args, **kwargs): raise CallbackEndpointError("connection_failed")
    monkeypatch.setattr("app.relay_client.send_callback", failed)
    monkeypatch.setattr("app.events.public_destination", forbid)
    monkeypatch.setattr("app.events.pinned_post", forbid)
    deliver(application.extensions["db_session"], application.config["TOKEN_ENCRYPTION_KEY"], relay_config=application.config)
    with application.extensions["db_session"]() as db:
        row = db.scalar(select(Delivery))
        assert not row.delivered and row.attempts == 1 and row.available_at > now()


def test_api_and_relay_share_generated_correlation_and_log_only_safe_fields(client, application, monkeypatch, caplog):
    enable(application)
    access = exchange(client, authorize(client)).json["access_token"]
    seen = []
    def failed(config, url, body, headers, operation, correlation_id):
        seen.append(correlation_id)
        raise CallbackEndpointError("timeout")
    monkeypatch.setattr("app.relay_client.send_callback", failed)
    caplog.set_level("INFO", logger="whatsapp.audit")
    result = rpc(client, access, "events/subscribe", params())
    assert result.json["error"] == {"code": -32015, "message": "CallbackEndpointError", "data": {"reason": "timeout"}}
    correlation = result.headers["X-Request-ID"]
    assert valid_correlation_id(correlation) and seen == [correlation]
    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "whatsapp.audit"]
    rpc_record = next(r for r in records if r["kind"] == "mcp_rpc_outcome")
    assert rpc_record["method"] == "events/subscribe" and rpc_record["rpc_code"] == -32015
    assert rpc_record["reason"] == "timeout" and rpc_record["correlation_id"] == correlation
    assert access not in caplog.text and "hidden-marker" not in caplog.text
    assert params()["delivery"]["secret"] not in caplog.text and "callback.test" not in caplog.text


def test_controller_tick_passes_relay_configuration(application, monkeypatch):
    from app import controller
    class Engine:
        def dispose(self): pass
    @contextmanager
    def locked(*args): yield True
    seen = []
    monkeypatch.setattr(controller, "database", lambda url: (Engine(), object()))
    monkeypatch.setattr(controller.signal, "signal", lambda *args: None)
    monkeypatch.setattr(controller, "controller_lock", locked)
    monkeypatch.setattr(controller, "deliver", lambda *a, **kw: seen.append(kw))
    enable(application)
    controller.webhook_tick(application.config)
    assert seen[0]["relay_config"]["WEBHOOK_RELAY_URL"] == "http://webhook-relay:8001"
