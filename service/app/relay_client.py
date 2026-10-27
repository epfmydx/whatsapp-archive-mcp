"""Bounded client for the configured private webhook relay; never falls back."""
import base64
import ipaddress
import json
from urllib.parse import urlsplit

import urllib3

from .relay import MAX_RESPONSE, TOKEN, _json, validate_payload


RELAY_DEADLINE = 22
MAX_RELAY_RESPONSE = 8192


class _InvalidDestination(Exception):
    pass


def _relay_post(base, token, payload):
    # PoolManager never consults HTTP_PROXY/HTTPS_PROXY environment settings.
    pool = urllib3.PoolManager(num_pools=1, maxsize=1)
    try:
        response = pool.request("POST", base + "/internal/webhooks/send", body=payload,
                                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                                timeout=urllib3.Timeout(connect=5, read=21), retries=False,
                                redirect=False, preload_content=False)
        try:
            return response.status, response.read(MAX_RELAY_RESPONSE + 1)
        finally:
            response.close()
    finally:
        pool.clear()


def send_callback(config, url, body, headers, operation="delivery", correlation_id=None):
    from .events import CallbackEndpointError, _bounded_call
    # A missing relay is a caller configuration error, never permission to send direct.
    base, token = config.get("WEBHOOK_RELAY_URL", ""), config.get("WEBHOOK_RELAY_TOKEN", "")
    try:
        if not isinstance(base, str) or len(base) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in base):
            raise ValueError("Invalid relay configuration")
        parsed = urlsplit(base)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Invalid relay configuration")
        parsed.port  # Reject malformed ports before starting transport.
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError("Invalid relay configuration")
        validate_payload(url, body, headers, operation, correlation_id)
    except (ValueError, TypeError):
        raise CallbackEndpointError("connection_failed") from None
    payload = json.dumps({"url": url, "body": base64.b64encode(body).decode("ascii"), "headers": headers,
                          "operation": operation, "correlation_id": correlation_id}, separators=(",", ":")).encode("utf-8")
    try:
        status, raw = _bounded_call(_relay_post, (base.rstrip("/"), token, payload), RELAY_DEADLINE)
        if type(status) is not int or not isinstance(raw, bytes) or len(raw) > MAX_RELAY_RESPONSE:
            raise OSError()
        result = _json(raw.decode("utf-8"))
        if not isinstance(result, dict) or result.get("correlation_id") != correlation_id:
            raise OSError()
        if status != 200:
            if set(result) != {"error", "correlation_id"}:
                raise OSError()
            if result["error"] == "invalid_destination" and status == 400:
                raise _InvalidDestination()
            if result["error"] == "timeout" and status == 503:
                raise TimeoutError()
            raise OSError()
        if (set(result) != {"status", "body", "destination_ip", "correlation_id"}
                or type(result["status"]) is not int or not 100 <= result["status"] <= 599
                or not isinstance(result["body"], str) or len(result["body"]) > 4 * ((MAX_RESPONSE + 2) // 3)
                or not isinstance(result["destination_ip"], str) or not ipaddress.ip_address(result["destination_ip"]).is_global):
            raise OSError()
        answer = base64.b64decode(result["body"], validate=True)
        if len(answer) > MAX_RESPONSE:
            raise OSError()
        return result["status"], answer, result["destination_ip"]
    except TimeoutError:
        raise CallbackEndpointError("timeout") from None
    except _InvalidDestination:
        raise ValueError("Invalid callback destination") from None
    except Exception:
        raise CallbackEndpointError("connection_failed") from None
