import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.config import settings
from app.observability import SafeGunicornLogger, log_callback, log_rpc, valid_correlation_id
from test_auth import authorize, exchange


def test_log_fields_cannot_capture_caller_strings(caplog):
    secret = "https://callback.test/private?token=hidden-log-marker\n"
    caplog.set_level("INFO", logger="whatsapp.audit")
    log_rpc(secret, secret, secret, secret, secret, rpc_code=secret, reason=secret)
    log_callback(secret, secret, secret, secret, callback_status=secret, reason=secret, component=secret)
    assert "hidden-log-marker" not in caplog.text
    for record in caplog.records:
        data = json.loads(record.getMessage())
        assert valid_correlation_id(data["correlation_id"]) and data["elapsed_ms"] == 0


def test_gunicorn_error_logging_never_formats_request_data(caplog):
    caplog.set_level("INFO", logger="whatsapp.audit")
    logger = object.__new__(SafeGunicornLogger)
    secret = "https://callback.test/private?token=hidden-server-marker"
    for method in ("warning", "error", "exception", "critical", "info", "debug", "access"):
        getattr(logger, method)("request %s", secret, exc_info=True)
    assert len(caplog.records) == 4 and "hidden-server-marker" not in caplog.text


def test_unknown_method_and_caller_ids_are_not_logged(client, caplog):
    access = exchange(client, authorize(client)).json["access_token"]
    caplog.set_level("INFO", logger="whatsapp.audit")
    result = client.post("/mcp", base_url="https://archive.test", headers={
        "Authorization": "Bearer "+access, "X-Request-ID": "hidden-caller-marker"}, json={
        "jsonrpc": "2.0", "id": "hidden-jsonrpc-marker", "method": "hidden-method-marker"})
    assert result.json["error"]["code"] == -32601
    record = json.loads(caplog.records[-1].getMessage())
    assert record["method"] == "unknown" and record["rpc_code"] == -32601
    assert record["correlation_id"] == result.headers["X-Request-ID"]
    assert not any(marker in caplog.text for marker in ("hidden-caller-marker", "hidden-jsonrpc-marker", "hidden-method-marker", access))


def test_authentication_failure_has_generated_correlation(client, caplog):
    caplog.set_level("INFO", logger="whatsapp.audit")
    response = client.post("/mcp", base_url="https://archive.test", json={"method": "private-marker"})
    assert response.status_code == 401 and valid_correlation_id(response.headers["X-Request-ID"])
    record = json.loads(caplog.records[-1].getMessage())
    assert record["http_status"] == 401 and record["outcome"] == "http_error"
    assert "private-marker" not in caplog.text


def test_unexpected_api_exception_does_not_log_private_exception_text(client, application, monkeypatch, caplog):
    access = exchange(client, authorize(client)).json["access_token"]
    application.config["TESTING"] = False
    def failed(*args, **kwargs):
        raise RuntimeError("hidden-provider-payload https://callback.test/?secret=hidden-query")
    monkeypatch.setattr("app.api.tool", failed)
    caplog.set_level("INFO")
    response = client.post("/mcp", base_url="https://archive.test", headers={"Authorization":"Bearer "+access},
                           json={"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{"name":"list_accounts"}})
    assert response.status_code == 500
    assert not any(marker in caplog.text for marker in ("hidden-provider-payload", "hidden-query", "callback.test"))
    records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "whatsapp.audit"]
    assert any(r.get("reason") == "internal_error" and r["correlation_id"] == response.headers["X-Request-ID"] for r in records)


def test_secondary_flask_finalization_failure_redacts_exception_and_traceback(client, application, monkeypatch, caplog):
    import app as app_module
    access = exchange(client, authorize(client)).json["access_token"]
    sessions, engine = application.extensions["db_session"], application.extensions["db_engine"]
    class FailingFinalizer:
        def __init__(self): self.db = sessions()
        def __getattr__(self, name): return getattr(self.db, name)
        def commit(self): raise RuntimeError("hidden-commit-parameters")
        def rollback(self): raise RuntimeError("hidden-rollback-provider https://callback.test/?secret=hidden-finalizer")
    monkeypatch.setattr(app_module, "database", lambda url: (engine, FailingFinalizer))
    config = dict(application.config, TESTING=False)
    failing = app_module.create_app(config)
    caplog.set_level("INFO")
    response = failing.test_client().post("/mcp", base_url="https://archive.test", headers={
        "Authorization":"Bearer "+access}, json={"jsonrpc":"2.0", "id":1, "method":"ping"})
    assert response.status_code == 500
    assert not any(marker in caplog.text for marker in (
        "hidden-commit-parameters", "hidden-rollback-provider", "hidden-finalizer", "callback.test"))
    framework = [r for r in caplog.records if r.name == failing.logger.name]
    assert framework and all(r.exc_info is None and r.exc_text is None and r.stack_info is None for r in framework)
    assert any(json.loads(r.getMessage()).get("kind") == "framework_error" for r in framework)
    rpc_records = [json.loads(r.getMessage()) for r in caplog.records if r.name == "whatsapp.audit"
                   and json.loads(r.getMessage()).get("kind") == "mcp_rpc_outcome"]
    assert len(rpc_records) == 1
    assert rpc_records[0]["http_status"] == 500 and rpc_records[0]["outcome"] == "http_error"


@pytest.mark.parametrize("changes", [
    {"WEBHOOK_RELAY_URL":"http://relay:8001"},
    {"WEBHOOK_RELAY_TOKEN":"r"*32},
    {"WEBHOOK_RELAY_URL":"http://relay:8001/path", "WEBHOOK_RELAY_TOKEN":"r"*32},
    {"WEBHOOK_RELAY_URL":"http://user:pass@relay:8001", "WEBHOOK_RELAY_TOKEN":"r"*32},
    {"WEBHOOK_RELAY_URL":"http://relay:8001?hidden", "WEBHOOK_RELAY_TOKEN":"r"*32},
    {"WEBHOOK_RELAY_URL":"http://relay:8001", "WEBHOOK_RELAY_TOKEN":"i"*32},
])
def test_bad_relay_configuration_fails_closed(application, changes):
    config = dict(application.config); config.update(changes)
    with pytest.raises(ValueError): settings(config)


def test_relay_configuration_preserves_disabled_defaults_and_dedicated_secret(application):
    assert application.config["WEBHOOK_RELAY_URL"] == "" and application.config["WEBHOOK_RELAY_TOKEN"] == ""
    config = dict(application.config)
    config.update(WEBHOOK_RELAY_URL="http://relay:8001/", WEBHOOK_RELAY_TOKEN="r"*32)
    assert settings(config)["WEBHOOK_RELAY_URL"] == "http://relay:8001"


def test_controller_audit_is_emitted_from_real_spawn_child(tmp_path):
    script = tmp_path/"spawn_audit.py"
    script.write_text('''
import logging
import multiprocessing
from contextlib import contextmanager
from app import controller
from app.observability import log_callback
class Engine:
    def dispose(self): pass
@contextmanager
def locked(*args): yield True
def dispatch(*args, **kwargs):
    log_callback("delivery", "success", "a"*32, 1, callback_status=204, component="controller")
controller.database = lambda url: (Engine(), object())
controller.controller_lock = locked
controller.deliver = dispatch
if __name__ == "__main__":
    logging.getLogger().setLevel(logging.WARNING)
    config={"DATABASE_URL":"synthetic", "TOKEN_ENCRYPTION_KEY":"synthetic", "OAUTH_CLIENTS":[],
            "ACCOUNTS":[], "OAUTH_DYNAMIC_REGISTRATION_ENABLED":True}
    child=multiprocessing.get_context("spawn").Process(target=controller.webhook_tick,args=(config,))
    child.start(); child.join(10)
    if child.is_alive(): child.terminate(); child.join(); raise RuntimeError("Fixture deadline")
    assert child.exitcode == 0
''')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=15, env=env)
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert any(r.get("component") == "controller" and r.get("correlation_id") == "a"*32 for r in records)
