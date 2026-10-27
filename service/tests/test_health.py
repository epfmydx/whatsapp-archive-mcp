"""Synthetic health receipts, separate from historic WhatsApp timestamps."""
import pytest

from app import health
from app.health import account_health, mark_ingested, update_health
from app.models import Account, AccountHealth
from test_identities import message, session, second_account

FULL = {"whatsapp_connected": True, "whatsapp_authenticated": True, "pending_count": 0, "oldest_pending_at": None, "error": None}


def test_missing_receipt_unknown_legacy_status_unknown(application, monkeypatch):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        result = account_health(account)
        assert result["health"] == "unknown"
        assert result["last_bridge_seen_at"] is None
        assert result["last_message_ingested_at"] is None
        update_health(account, {"status": "connected", "error": None})
        result = account_health(account)
        assert result["health"] == "unknown"
        assert result["whatsapp_connected"] is None
        assert result["whatsapp_authenticated"] is None
        assert result["pending_count"] is None


def test_each_unchanged_status_records_receipt_and_error_capture(application, monkeypatch):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        original_updated = account.updated_at
        update_health(account, FULL)
        monkeypatch.setattr(health, "now", lambda: 1030)
        update_health(account, FULL)
        row = db.get(AccountHealth, "personal")
        assert row.last_bridge_seen_at == 1030
        assert account.updated_at == original_updated
        update_health(account, {"error": "archive_or_ingestion_pending"})
        assert account_health(account)["error"] == "archive_or_ingestion_pending"
        assert account_health(account)["health"] == "backlog"


def test_quiet_old_archive_healthy_receipt_ingestion_and_message_dates_differ(application, monkeypatch):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        message(db, timestamp="2000-01-01T00:00:00.000000Z")
        mark_ingested("personal")
        monkeypatch.setattr(health, "now", lambda: 2000)
        update_health(account, FULL)
        result = account_health(account)
        assert result["health"] == "healthy"
        assert result["latest_message_at"] == "2000-01-01T00:00:00.000000Z"
        assert result["last_message_ingested_at"] == "1970-01-01T00:16:40Z"
        assert result["last_bridge_seen_at"] == "1970-01-01T00:33:20Z"
        monkeypatch.setattr(health, "now", lambda: 2090)
        assert account_health(account)["health"] == "healthy"
        monkeypatch.setattr(health, "now", lambda: 2091)
        assert account_health(account)["health"] == "stale"


@pytest.mark.parametrize("fields,state", [
    ({"whatsapp_connected": False}, "disconnected"),
    ({"whatsapp_authenticated": False}, "disconnected"),
    ({"pending_count": 1}, "backlog"),
    ({"oldest_pending_at": "2026-10-26T20:00:00+08:00"}, "backlog"),
    ({"whatsapp_authenticated": None}, "unknown"),
    ({"pending_count": None}, "unknown"),
])
def test_connection_backlog_and_incomplete_metadata(application, monkeypatch, fields, state):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        update_health(account, FULL | fields)
        assert account_health(account)["health"] == state
        if "oldest_pending_at" in fields:
            assert account_health(account)["oldest_pending_at"] == "2026-10-26T12:00:00.000000Z"


@pytest.mark.parametrize("bad", [
    {"whatsapp_connected": 1}, {"whatsapp_authenticated": "true"},
    {"pending_count": -1}, {"pending_count": True}, {"pending_count": 2**31},
    {"oldest_pending_at": "no-date"}, {"oldest_pending_at": "2026-10-26T12:00:00"},
    {"error": "raw private error"},
])
def test_invalid_health_rejected_without_receipt_or_partial_write(application, monkeypatch, bad):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        with pytest.raises(ValueError):
            update_health(account, FULL | bad)
        assert db.get(AccountHealth, "personal") is None


def test_health_account_isolation_and_mark_ingested_does_not_fabricate_receipt(application, monkeypatch):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        second_account(application, db)
        update_health(db.get(Account, "personal"), FULL)
        mark_ingested("work")
        result = account_health(db.get(Account, "work"))
        assert result["health"] == "unknown" and result["last_bridge_seen_at"] is None
        assert result["last_message_ingested_at"] is not None
        assert account_health(db.get(Account, "personal"))["last_message_ingested_at"] is None
        with pytest.raises(ValueError):
            mark_ingested("unknown")


def test_legacy_receipt_clears_old_complete_telemetry(application, monkeypatch):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        update_health(account, FULL)
        assert account_health(account)["health"] == "healthy"
        monkeypatch.setattr(health, "now", lambda: 1030)
        update_health(account, {"status": "connected", "error": None})
        result = account_health(account)
        assert result["health"] == "unknown"
        assert result["whatsapp_connected"] is None
        assert result["whatsapp_authenticated"] is None
        assert result["pending_count"] is None
        assert db.get(AccountHealth, "personal").last_bridge_seen_at == 1030


def test_identity_only_pending_error_is_visible_as_backlog(application, monkeypatch):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        update_health(account, FULL)
        assert account_health(account)["health"] == "healthy"
        update_health(account, FULL | {"error": "identity_reconciliation_pending"})
        result = account_health(account)
        assert result["health"] == "backlog"
        assert result["pending_count"] == 0
        assert result["error"] == "identity_reconciliation_pending"


def test_ingestion_refreshes_preloaded_health_after_account_lock(application, monkeypatch):
    from sqlalchemy import update
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        update_health(account, FULL)
        row = db.get(AccountHealth, "personal")
        db.execute(update(AccountHealth).where(AccountHealth.account_id == "personal").values(
            error="identity_reconciliation_pending").execution_options(synchronize_session=False))
        assert row.error is None
        mark_ingested("personal")
        assert row.error == "identity_reconciliation_pending"
        assert row.last_message_ingested_at == 1000
