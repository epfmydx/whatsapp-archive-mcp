"""Private, credential-minimal transport for signed webhook POSTs."""
import base64
import binascii
from collections import deque
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException
from .observability import SafeGunicornLogger, install_safe_framework_logging


MAX_BODY = 64 * 1024
MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 4097
CORRELATION = re.compile(r"[0-9a-f]{32}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
HEADER_NAMES = {"content-type", "webhook-id", "webhook-timestamp", "webhook-signature", "x-mcp-subscription-id"}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid relay request")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_json_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid JSON")))


def validate_payload(url, body, headers, operation, correlation_id):
    """Validate the signed callback envelope without interpreting event data."""
    if (not isinstance(url, str) or not 1 <= len(url) <= 2048
            or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise ValueError("Invalid relay request")
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_BODY:
        raise ValueError("Invalid relay request")
    if operation not in ("verification", "delivery") or not isinstance(correlation_id, str) or not CORRELATION.fullmatch(correlation_id):
        raise ValueError("Invalid relay request")
    if not isinstance(headers, dict) or len(headers) != len(HEADER_NAMES):
        raise ValueError("Invalid relay request")
    normalized = {}
    for key, value in headers.items():
        if (not isinstance(key, str) or key.lower() not in HEADER_NAMES or key.lower() in normalized
                or not isinstance(value, str) or not 1 <= len(value) <= (1024 if key.lower() == "webhook-signature" else 512)
                or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise ValueError("Invalid relay request")
        normalized[key.lower()] = value
    if set(normalized) != HEADER_NAMES or normalized["content-type"] != "application/json":
        raise ValueError("Invalid relay request")
    try:
        payload = _json(body.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid relay request") from None
    if not isinstance(payload, dict):
        raise ValueError("Invalid relay request")
    if operation == "verification":
        if payload.get("type") != "verification" or not isinstance(payload.get("challenge"), str) or not 1 <= len(payload["challenge"]) <= 512:
            raise ValueError("Invalid relay request")
    elif (not all(isinstance(payload.get(key), str) and payload[key] for key in ("eventId", "name", "timestamp"))
          or "data" not in payload):
        raise ValueError("Invalid relay request")


class Admission:
    """One immediate outbound attempt; a bounded sliding-window rate budget."""
    def __init__(self, limit, clock=time.monotonic):
        self.limit, self.clock = limit, clock
        self.lock = threading.Lock()
        self.attempts = deque(maxlen=limit)
        self.busy = False

    def acquire(self):
        with self.lock:
            now = self.clock()
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if self.busy or len(self.attempts) >= self.limit:
                return False
            self.attempts.append(now)
            self.busy = True
            return True

    def release(self):
        with self.lock:
            self.busy = False


def _log(operation, outcome, correlation_id, started, callback_status=None, reason=None):
    from .observability import log_callback
    log_callback(operation, outcome, correlation_id, int((time.monotonic() - started) * 1000),
                 callback_status=callback_status, reason=reason)


class RelayLogger(SafeGunicornLogger):
    component = "relay"


def create_app(overrides=None):
    # Deliberately do not call the application's settings/database factory.
    config = {"WEBHOOK_RELAY_TOKEN": os.environ.get("WEBHOOK_RELAY_TOKEN", ""),
              "WEBHOOK_RELAY_MAX_REQUESTS_PER_MINUTE": os.environ.get("WEBHOOK_RELAY_MAX_REQUESTS_PER_MINUTE", "60")}
    config.update(overrides or {})
    if not isinstance(config["WEBHOOK_RELAY_TOKEN"], str) or not TOKEN.fullmatch(config["WEBHOOK_RELAY_TOKEN"]):
        raise ValueError("Invalid WEBHOOK_RELAY_TOKEN")
    rate = config["WEBHOOK_RELAY_MAX_REQUESTS_PER_MINUTE"]
    if isinstance(rate, str) and rate.isascii() and rate.isdecimal():
        rate = int(rate)
    if type(rate) is not int or not 1 <= rate <= 3600:
        raise ValueError("Invalid WEBHOOK_RELAY_MAX_REQUESTS_PER_MINUTE")
    app = Flask(__name__)
    install_safe_framework_logging(app, "relay")
    app.config.update(config, MAX_CONTENT_LENGTH=MAX_REQUEST)
    admission = Admission(rate)
    app.extensions["relay_admission"] = admission

    @app.get("/healthz")
    def health():
        return "ok", 200, {"Content-Type": "text/plain"}

    @app.post("/internal/webhooks/send")
    def send():
        started, correlation, operation = time.monotonic(), uuid.uuid4().hex, "unknown"
        g.correlation_id = correlation

        def fail(reason, status):
            _log(operation, "error", correlation, started, reason=reason)
            return jsonify(error=reason, correlation_id=correlation), status

        authorization = request.headers.get("Authorization", "")
        expected = "Bearer " + app.config["WEBHOOK_RELAY_TOKEN"]
        if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("ascii")):
            return fail("connection_failed", 401)
        try:
            if request.mimetype != "application/json" or request.query_string:
                return fail("invalid_destination", 400)
            envelope = _json(request.get_data(cache=False).decode("utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {"url", "body", "headers", "operation", "correlation_id"}:
                return fail("invalid_destination", 400)
            if not isinstance(envelope["body"], str) or len(envelope["body"]) > 4 * ((MAX_BODY + 2) // 3):
                return fail("invalid_destination", 400)
            body = base64.b64decode(envelope["body"], validate=True)
            validate_payload(envelope["url"], body, envelope["headers"], envelope["operation"], envelope["correlation_id"])
            correlation, operation = envelope["correlation_id"], envelope["operation"]
            g.correlation_id = correlation
        except (ValueError, UnicodeError, TypeError, RecursionError, binascii.Error, HTTPException):
            return fail("invalid_destination", 400)
        if not admission.acquire():
            return fail("capacity", 429)
        try:
            from .events import CallbackEndpointError, public_destination, pinned_post
            ip = public_destination(envelope["url"])
            status, answer = pinned_post(envelope["url"], ip, body, envelope["headers"])
            if type(status) is not int or not 100 <= status <= 599 or not isinstance(answer, bytes) or len(answer) > MAX_RESPONSE:
                return fail("connection_failed", 503)
            _log(operation, "success", correlation, started, callback_status=status)
            return jsonify(status=status, body=base64.b64encode(answer).decode("ascii"), destination_ip=ip, correlation_id=correlation)
        except CallbackEndpointError as error:
            return fail("timeout" if error.reason == "timeout" else "connection_failed", 503)
        except TimeoutError:
            return fail("timeout", 503)
        except ValueError:
            return fail("invalid_destination", 400)
        except Exception:
            return fail("connection_failed", 503)
        finally:
            admission.release()

    @app.errorhandler(Exception)
    def sanitized_error(error):
        correlation = getattr(g, "correlation_id", None) or uuid.uuid4().hex
        status = error.code if isinstance(error, HTTPException) else 503
        _log("unknown", "error", correlation, time.monotonic(), reason="connection_failed")
        return jsonify(error="connection_failed", correlation_id=correlation), status

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def main():
    from gunicorn.app.base import BaseApplication
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    class Server(BaseApplication):
        def load_config(self):
            for key, value in {"bind": "0.0.0.0:8001", "workers": 1, "threads": 4, "timeout": 30,
                               "accesslog": None, "errorlog": "-", "forwarded_allow_ips": "",
                               "secure_scheme_headers": {}, "logger_class": RelayLogger}.items():
                self.cfg.set(key, value)

        def load(self):
            return create_app()

    Server().run()


if __name__ == "__main__":
    main()
