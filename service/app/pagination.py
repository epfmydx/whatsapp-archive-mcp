"""Signed, filter-bound keyset cursors for the mutable local archive."""
import hashlib
import hmac
import json
from datetime import datetime, timezone

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer

CURSOR_MAX_LENGTH = 4096
CURSOR_MAX_AGE = 24 * 60 * 60
CURSOR_VERSION = 1


def normalized_timestamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise ValueError("Invalid timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise ValueError("Invalid timestamp") from None


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="whatsapp-archive-keyset-v1")


def cursor_context(owner, account_id, tool, filters):
    # Query text and other private filter values never appear in the cursor payload.
    encoded = json.dumps([owner, account_id, tool, filters], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def encode_cursor(context, timestamp, ident):
    return _serializer().dumps({"v": CURSOR_VERSION, "c": context, "p": {"timestamp": timestamp, "id": ident}})


def decode_cursor(value, context):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= CURSOR_MAX_LENGTH:
            raise ValueError()
        payload = _serializer().loads(value, max_age=CURSOR_MAX_AGE)
        if not isinstance(payload, dict) or set(payload) != {"v", "c", "p"}:
            raise ValueError()
        if type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION:
            raise ValueError()
        if not isinstance(payload["c"], str) or not hmac.compare_digest(payload["c"], context):
            raise ValueError()
        position = payload["p"]
        if not isinstance(position, dict) or set(position) != {"timestamp", "id"}:
            raise ValueError()
        if not isinstance(position["id"], str) or not 1 <= len(position["id"]) <= 300:
            raise ValueError()
        normalized_timestamp(position["timestamp"])
        return position
    except (BadData, ValueError, TypeError, OverflowError):
        raise ValueError("Invalid or expired cursor") from None


def page(items, limit, context, position):
    has_more = len(items) > limit
    visible = items[:limit]
    next_cursor = encode_cursor(context, *position(visible[-1])) if has_more else None
    return {"items": visible, "next_cursor": next_cursor, "has_more": has_more}
