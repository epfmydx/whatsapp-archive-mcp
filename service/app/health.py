"""Bridge receipt and ingestion health, separate from archive message dates."""
import json
from datetime import datetime, timezone

from flask import g
from sqlalchemy import func, select

from .identities import observed_timestamp, serialize_account
from .models import AccountHealth, AccountQueueHealth, Message, now
from .profiles import require_account

STALE_AFTER_SECONDS = 90
MAX_COUNT = 2**31 - 1
MAX_SNAPSHOT_BYTES = 16384
# Keep in sync with bridge/diagnostics.go safeReason. Never persist raw errors.
RETRY_REASONS = frozenset((
    "unsupported_archive", "ok", "unknown", "invalid_metadata", "missing_metadata",
    "bound_exceeded", "integrity_hash", "integrity_hmac", "integrity_ciphertext",
    "too_short", "missing_url", "unsupported_media", "nothing_downloadable", "http_status",
    "timeout", "cancelled", "network", "tls", "filesystem", "storage_full", "permission",
    "not_found", "encoding", "request", "s3_access_denied", "s3_not_found", "s3_throttled",
    "s3_invalid_request", "s3_unavailable", "protocol_denied", "protocol_rate_limited",
    "protocol_unavailable", "protocol_error", "disconnected",
    "recovery_timeout", "recovery_unavailable", "recovery_no_key", "recovery_rejected",
    "recovery_invalid_path", "recovery_exhausted", "download_exhausted", "requeued",
))
QUEUE_LANES = ("text", "media", "unknown_lane")


def _object(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("Invalid bridge health snapshot")
    return value


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ValueError("Invalid bridge health count")
    return value


def _timestamp(value):
    return observed_timestamp(value) if value is not None else None


def _reason(value):
    if not isinstance(value, str) or value not in RETRY_REASONS:
        raise ValueError("Invalid bridge health reason")
    return value


def _retry_snapshot(value):
    if value is None:
        return None
    _object(value, ("due", "cooling_down", "failed", "errors_by_code", "errors_by_http_status",
                    "earliest_next_at", "last_failure_at"),
            ("last_failure_reason", "quarantined", "awaiting_media_retry", "quarantine_reasons"))
    result = {key: _count(value[key]) for key in ("due", "cooling_down", "failed")}
    codes = _object(value["errors_by_code"], (), RETRY_REASONS)
    result["errors_by_code"] = {_reason(key): _count(count) for key, count in codes.items()}
    statuses = _object(value["errors_by_http_status"], (), (str(n) for n in range(100, 600)))
    result["errors_by_http_status"] = {key: _count(count) for key, count in statuses.items()}
    for key in ("earliest_next_at", "last_failure_at"):
        result[key] = _timestamp(value[key])
    reason = value.get("last_failure_reason")
    result["last_failure_reason"] = _reason(reason) if reason is not None else None
    # Additive telemetry: omission by older bridges means unknown, not zero.
    for key in ("quarantined", "awaiting_media_retry"):
        if key in value:
            result[key] = _count(value[key])
    if "quarantine_reasons" in value:
        reasons = _object(value["quarantine_reasons"], (), RETRY_REASONS)
        result["quarantine_reasons"] = {_reason(key): _count(count) for key, count in reasons.items()}
        total = sum(result["quarantine_reasons"].values())
        if total > MAX_COUNT or ("quarantined" in result and total != result["quarantined"]):
            raise ValueError("Invalid bridge health quarantine totals")
    return result


def _queue_snapshot(value):
    if value is None:
        return None
    _object(value, (*QUEUE_LANES, "completed_since_start", "last_successful_processing_at",
                    "completion_scope", "retained_unsupported_count"))
    if value["completion_scope"] != "process_lifetime":
        raise ValueError("Invalid bridge health completion scope")
    result = {"completion_scope": "process_lifetime"}
    for key in QUEUE_LANES:
        lane = _object(value[key], ("pending", "oldest_captured_at", "unknown_capture_timestamps", "origins"))
        pending = _count(lane["pending"])
        unknown = _count(lane["unknown_capture_timestamps"])
        origins = _object(lane["origins"], ("live", "history", "unknown"))
        origins = {origin: _count(count) for origin, count in origins.items()}
        oldest = _timestamp(lane["oldest_captured_at"])
        if unknown > pending or sum(origins.values()) != pending or (oldest is None) != (unknown == pending):
            raise ValueError("Invalid bridge health lane totals")
        result[key] = {"pending": pending, "oldest_captured_at": oldest,
                       "unknown_capture_timestamps": unknown, "origins": origins}
    total = sum(result[key]["pending"] for key in QUEUE_LANES)
    if total > MAX_COUNT:
        raise ValueError("Invalid bridge health queue total")
    completed = _object(value["completed_since_start"], ("text", "media"))
    result["completed_since_start"] = {key: _count(count) for key, count in completed.items()}
    result["last_successful_processing_at"] = _timestamp(value["last_successful_processing_at"])
    result["retained_unsupported_count"] = _count(value["retained_unsupported_count"])
    return result


def _encode_snapshot(value):
    if value is None:
        return None
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Invalid bridge health snapshot size")
    return encoded


def _row(account_id):
    serialize_account(account_id)
    row = g.db.get(AccountHealth, account_id, populate_existing=True)
    if row is None:
        row = AccountHealth(account_id=account_id)
        g.db.add(row)
    return row


def update_health(account, data):
    require_account(account.id)
    if not isinstance(data, dict):
        raise ValueError("Invalid bridge health")
    # Each receipt is a full telemetry snapshot. A legacy bridge that omits
    # fields must not keep a previous version's connected/empty-queue signals.
    fields = {key: None for key in ("whatsapp_connected", "whatsapp_authenticated",
                                   "pending_count", "oldest_pending_at", "error")}
    for key in ("whatsapp_connected", "whatsapp_authenticated"):
        if key in data:
            value = data[key]
            if value is not None and type(value) is not bool:
                raise ValueError("Invalid bridge health")
            fields[key] = value
    if "pending_count" in data:
        value = data["pending_count"]
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_COUNT):
            raise ValueError("Invalid bridge health")
        fields["pending_count"] = value
    if "oldest_pending_at" in data:
        value = data["oldest_pending_at"]
        fields["oldest_pending_at"] = observed_timestamp(value) if value is not None else None
    if "error" in data:
        if data["error"] not in (None, "archive_or_ingestion_pending", "identity_reconciliation_pending"):
            raise ValueError("Invalid bridge health error")
        fields["error"] = data["error"]
    # Validate the full receipt before touching either table. Omission clears a
    # previous bridge version's snapshot rather than claiming it is still current.
    retry_snapshot = _encode_snapshot(_retry_snapshot(data.get("retry_health")))
    queue_snapshot = _encode_snapshot(_queue_snapshot(data.get("queue_health")))
    row = _row(account.id)
    queue_row = g.db.get(AccountQueueHealth, account.id, populate_existing=True)
    if queue_row is None:
        queue_row = AccountQueueHealth(account_id=account.id)
        g.db.add(queue_row)
    queue_row.retry_health = retry_snapshot
    queue_row.queue_health = queue_snapshot
    row.last_bridge_seen_at = now()
    for key, value in fields.items():
        setattr(row, key, value)
    g.db.flush()
    return row


def mark_ingested(account_id):
    row = _row(account_id)
    row.last_message_ingested_at = now()
    g.db.flush()


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if value is not None else None


def account_health(account):
    require_account(account.id)
    row = g.db.get(AccountHealth, account.id)
    fields = {key: getattr(row, key) if row else None for key in (
        "last_bridge_seen_at", "last_message_ingested_at", "whatsapp_connected", "whatsapp_authenticated",
        "pending_count", "oldest_pending_at", "error")}
    queue_row = g.db.get(AccountQueueHealth, account.id)
    for key in ("retry_health", "queue_health"):
        value = getattr(queue_row, key) if queue_row else None
        fields[key] = json.loads(value) if value is not None else None
    retry = fields["retry_health"] or {}
    queue = fields["queue_health"] or {}
    snapshot_pending = (retry.get("due", 0) + retry.get("cooling_down", 0) + retry.get("failed", 0) > 0 or
                        any(queue.get(lane, {}).get("pending", 0) > 0 for lane in QUEUE_LANES))
    seen = fields["last_bridge_seen_at"]
    if seen is None:
        state = "unknown"
    elif now()-seen > STALE_AFTER_SECONDS:
        state = "stale"
    elif fields["whatsapp_connected"] is False or fields["whatsapp_authenticated"] is False:
        state = "disconnected"
    elif fields["error"] or (fields["pending_count"] is not None and fields["pending_count"] > 0) or fields["oldest_pending_at"] or snapshot_pending:
        state = "backlog"
    elif fields["whatsapp_connected"] is True and fields["whatsapp_authenticated"] is True and fields["pending_count"] == 0:
        state = "healthy"
    else:
        state = "unknown"
    fields["last_bridge_seen_at"] = _iso(seen)
    fields["last_message_ingested_at"] = _iso(fields["last_message_ingested_at"])
    fields["latest_message_at"] = g.db.scalar(select(func.max(Message.timestamp)).where(Message.account_id == account.id))
    return {**fields, "health": state}
