"""Allowlisted operational metadata; never log request/provider content."""
import json
import logging
import math
import re
import uuid
from gunicorn.glogging import Logger
from flask import g, has_request_context

LOG = logging.getLogger("whatsapp.audit")
CORRELATION = re.compile(r"[0-9a-f]{32}\Z")
RPC_METHODS = frozenset(("initialize", "server/discover", "ping", "tools/list", "tools/call",
                         "events/list", "events/subscribe", "events/unsubscribe",
                         "notifications/initialized", "notifications/cancelled"))
REASONS = frozenset(("timeout", "connection_failed", "invalid_destination", "capacity",
                     "http_failure", "challenge_failed", "unauthorized", "invalid_request",
                     "body_too_large", "rate_limited", "response_invalid", "internal_error"))
OUTCOMES = frozenset(("success", "error", "rejected", "http_failure", "unauthorized",
                      "invalid_request", "rate_limited", "capacity", "timeout",
                      "connection_failed", "invalid_destination", "http_error"))


def new_correlation_id():
    return uuid.uuid4().hex


def valid_correlation_id(value):
    return isinstance(value, str) and CORRELATION.fullmatch(value) is not None


def _choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unknown"


def _elapsed(value):
    return int(max(0, min(60000, value))) if type(value) in (int, float) and math.isfinite(value) else 0


def _base(kind, correlation_id, elapsed_ms):
    return {"kind": kind,
            "correlation_id": correlation_id if valid_correlation_id(correlation_id) else new_correlation_id(),
            "elapsed_ms": _elapsed(elapsed_ms)}


def log_callback(operation, outcome, correlation_id, elapsed_ms, callback_status=None,
                 reason=None, component="relay"):
    data = _base("webhook_outcome", correlation_id, elapsed_ms)
    data.update(component=_choice(component, {"api", "controller", "relay", "client"}),
                operation=_choice(operation, {"verification", "delivery"}),
                outcome=_choice(outcome, OUTCOMES))
    if type(callback_status) is int and 100 <= callback_status <= 599:
        data["callback_status"] = callback_status
    if reason is not None:
        data["reason"] = _choice(reason, REASONS)
    LOG.info("%s", json.dumps(data, sort_keys=True, separators=(",", ":")))


def log_rpc(method, outcome, correlation_id, elapsed_ms, http_status, rpc_code=None, reason=None):
    data = _base("mcp_rpc_outcome", correlation_id, elapsed_ms)
    data.update(component="api", method=_choice(method, RPC_METHODS), outcome=_choice(outcome, OUTCOMES))
    if type(http_status) is int and 100 <= http_status <= 599:
        data["http_status"] = http_status
    if type(rpc_code) is int and rpc_code in (-32600, -32601, -32602, -32603, -32015):
        data["rpc_code"] = rpc_code
    if reason is not None:
        data["reason"] = _choice(reason, REASONS)
    LOG.info("%s", json.dumps(data, sort_keys=True, separators=(",", ":")))


class SafeGunicornLogger(Logger):
    """Parser/lifecycle exceptions can contain URIs, headers and peer details."""
    component = "api"

    def _server_failure(self):
        log_callback("unknown", "error", new_correlation_id(), 0,
                     reason="internal_error", component=self.component)

    def warning(self, *args, **kwargs):
        self._server_failure()

    warn = warning

    def error(self, *args, **kwargs):
        self._server_failure()

    def exception(self, *args, **kwargs):
        self._server_failure()

    def critical(self, *args, **kwargs):
        self._server_failure()

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def access(self, *args, **kwargs):
        pass


class SafeFrameworkLogFilter(logging.Filter):
    """Redact secondary Flask finalization logs as well as primary errors."""
    def __init__(self, component):
        super().__init__()
        self.component = _choice(component, {"api", "relay"})

    def filter(self, record):
        correlation = getattr(g, "correlation_id", None) if has_request_context() else None
        data = _base("framework_error", correlation, 0)
        data.update(component=self.component, reason="internal_error")
        record.msg = json.dumps(data, sort_keys=True, separators=(",", ":"))
        record.args, record.exc_info, record.exc_text, record.stack_info = (), None, None, None
        record.message = record.msg
        return True


def install_safe_framework_logging(app, component="api"):
    if not any(isinstance(item, SafeFrameworkLogFilter) for item in app.logger.filters):
        app.logger.addFilter(SafeFrameworkLogFilter(component))
