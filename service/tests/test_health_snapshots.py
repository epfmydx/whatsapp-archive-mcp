"""Synthetic bounded queue snapshots; no provider or deployed database access."""
from copy import deepcopy

import pytest
from sqlalchemy import inspect

from app import health
from app.health import account_health, update_health
from app.init_db import initialize
from app.models import Account, AccountHealth, AccountQueueHealth
from test_auth import BASE
from test_health import FULL
from test_identities import second_account, session

STAMP = "2026-10-27T12:34:56.123456789+08:00"
NORMALIZED = "2026-10-27T04:34:56.123456Z"


def lane(pending=0, unknown=0):
    return {"pending": pending, "unknown_capture_timestamps": unknown,
            "oldest_captured_at": STAMP if pending > unknown else None,
            "origins": {"live": pending - unknown, "history": 0, "unknown": unknown}}


def receipt():
    return FULL | {
        "pending_count": 4,
        "retry_health": {"due": 1, "cooling_down": 3, "failed": 2,
                         "errors_by_code": {"timeout": 1, "unsupported_archive": 1},
                         "errors_by_http_status": {"504": 1},
                         "earliest_next_at": STAMP, "last_failure_at": STAMP,
                         "last_failure_reason": "timeout"},
        "queue_health": {"text": lane(1), "media": lane(2, 1), "unknown_lane": lane(1, 1),
                         "completed_since_start": {"text": 8, "media": 2},
                         "last_successful_processing_at": STAMP, "completion_scope": "process_lifetime",
                         "retained_unsupported_count": 7},
    }


def replace(data, path, value):
    for key in path[:-1]:
        data = data[key]
    data[path[-1]] = value


def test_queue_and_retry_roundtrip_persist_normalized_safe_aggregates(application, monkeypatch):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        update_health(db.get(Account, "personal"), receipt())
    with session(application) as db:
        result = account_health(db.get(Account, "personal"))
        assert result["health"] == "backlog"
        assert result["queue_health"]["media"] == lane(2, 1) | {"oldest_captured_at": NORMALIZED}
        assert result["queue_health"]["unknown_lane"]["oldest_captured_at"] is None
        assert result["queue_health"]["last_successful_processing_at"] == NORMALIZED
        assert result["queue_health"]["completed_since_start"] == {"text": 8, "media": 2}
        assert result["retry_health"]["earliest_next_at"] == NORMALIZED
        assert result["retry_health"]["errors_by_http_status"] == {"504": 1}
        assert result["last_bridge_seen_at"] == "1970-01-01T00:16:40Z"
        assert db.get(AccountQueueHealth, "personal") is not None
        monkeypatch.setattr(health, "now", lambda: 1091)
        assert account_health(db.get(Account, "personal"))["health"] == "stale"


@pytest.mark.parametrize("path,value", [
    (("retry_health",), []), (("queue_health",), "private payload"),
    (("retry_health", "due"), True), (("retry_health", "due"), -1),
    (("retry_health", "failed"), 2**31), (("retry_health", "cooling_down"), 1.0),
    (("retry_health", "errors_by_code"), {"https://private.example/message": 1}),
    (("retry_health", "errors_by_code"), {"timeout": "1"}),
    (("retry_health", "errors_by_code"), {"timeout": -1}),
    (("retry_health", "errors_by_http_status"), {"099": 1}),
    (("retry_health", "errors_by_http_status"), {"600": 1}),
    (("retry_health", "errors_by_http_status"), {"0504": 1}),
    (("retry_health", "errors_by_http_status"), {"504": True}),
    (("retry_health", "last_failure_reason"), "private exception"),
    (("retry_health", "last_failure_reason"), {"error": "private"}),
    (("retry_health", "last_failure_at"), "2026-10-27T12:00:00"),
    (("retry_health", "earliest_next_at"), 123456789),
    (("retry_health", "last_failure_at"), "x" * 20000),
    (("retry_health", "private_extra"), "secret"),
    (("queue_health", "private_extra"), "secret"),
    (("queue_health", "text", "origins", "private"), 1),
    (("queue_health", "text", "origins", "live"), False),
    (("queue_health", "text", "origins", "live"), 2),
    (("queue_health", "text", "unknown_capture_timestamps"), 2),
    (("queue_health", "text", "oldest_captured_at"), None),
    (("queue_health", "unknown_lane", "oldest_captured_at"), STAMP),
    (("queue_health", "text", "pending"), "1"),
    (("queue_health", "text", "pending"), 2**31),
    (("queue_health", "media", "oldest_captured_at"), "not a date"),
    (("queue_health", "completed_since_start", "text"), True),
    (("queue_health", "completed_since_start", "unknown_lane"), 1),
    (("queue_health", "retained_unsupported_count"), -1),
    (("queue_health", "completion_scope"), "since_whatsapp_message_date"),
    (("queue_health", "last_successful_processing_at"), "2026-10-27"),
])
def test_malformed_private_oversized_snapshots_never_write(application, path, value):
    bad = receipt()
    replace(bad, path, value)
    with session(application) as db:
        with pytest.raises(ValueError):
            update_health(db.get(Account, "personal"), bad)
        assert db.get(AccountHealth, "personal") is None
        assert db.get(AccountQueueHealth, "personal") is None


def test_bad_snapshot_preserves_both_existing_rows_and_receipt(application, monkeypatch):
    monkeypatch.setattr(health, "now", lambda: 1000)
    with session(application) as db:
        account = db.get(Account, "personal")
        update_health(account, receipt())
        before = deepcopy(account_health(account))
        monkeypatch.setattr(health, "now", lambda: 1030)
        bad = receipt()
        bad["queue_health"]["text"]["pending"] = False
        with pytest.raises(ValueError):
            update_health(account, bad)
        assert account_health(account) == before


def test_recovery_health_roundtrip_and_legacy_omission(application):
    data = receipt()
    data["retry_health"].update(quarantined=2, awaiting_media_retry=1,
                                quarantine_reasons={"http_status": 1, "bound_exceeded": 1})
    with session(application) as db:
        account = db.get(Account, "personal")
        update_health(account, data)
        result = account_health(account)["retry_health"]
        assert result["quarantined"] == 2
        assert result["awaiting_media_retry"] == 1
        assert result["quarantine_reasons"] == {"http_status": 1, "bound_exceeded": 1}
        update_health(account, receipt())
        result = account_health(account)["retry_health"]
        assert not {"quarantined", "awaiting_media_retry", "quarantine_reasons"} & result.keys()


@pytest.mark.parametrize("fields", [
    {"quarantined": True}, {"quarantined": -1}, {"quarantined": 2**31},
    {"awaiting_media_retry": "1"}, {"awaiting_media_retry": False},
    {"awaiting_media_retry": 2**31}, {"quarantine_reasons": []},
    {"quarantine_reasons": {"private provider message": 1}},
    {"quarantine_reasons": {"http_status": True}},
    {"quarantine_reasons": {"http_status": -1}},
    {"quarantined": 2, "quarantine_reasons": {"http_status": 1}},
    {"quarantine_reasons": {"http_status": 2**31-1, "timeout": 1}},
])
def test_recovery_health_rejects_invalid_values_atomically(application, fields):
    with session(application) as db:
        account = db.get(Account, "personal")
        update_health(account, receipt())
        before = deepcopy(account_health(account))
        data = receipt()
        data["retry_health"].update(fields)
        with pytest.raises(ValueError):
            update_health(account, data)
        assert account_health(account) == before


@pytest.mark.parametrize("key", ["retry_health", "queue_health"])
def test_snapshot_requires_complete_nested_object(application, key):
    bad = receipt()
    del bad[key][next(iter(bad[key]))]
    with session(application) as db:
        with pytest.raises(ValueError):
            update_health(db.get(Account, "personal"), bad)


@pytest.mark.parametrize("legacy", [{"status": "connected"}, FULL,
                                    FULL | {"retry_health": None, "queue_health": None}])
def test_legacy_omission_and_explicit_null_reset_old_snapshots(application, legacy):
    with session(application) as db:
        account = db.get(Account, "personal")
        assert account_health(account)["queue_health"] is None
        assert account_health(account)["retry_health"] is None
        update_health(account, receipt())
        update_health(account, legacy)
        result = account_health(account)
        assert result["retry_health"] is None
        assert result["queue_health"] is None
        assert db.get(AccountQueueHealth, "personal").queue_health is None


def test_snapshot_account_isolation_and_omission_clear_independently(application):
    with session(application) as db:
        second_account(application, db)
        personal = db.get(Account, "personal")
        work = db.get(Account, "work")
        update_health(personal, receipt())
        assert account_health(work)["retry_health"] is None
        assert account_health(work)["queue_health"] is None
        only_retry = receipt()
        del only_retry["queue_health"]
        update_health(work, only_retry)
        assert account_health(work)["retry_health"] is not None
        assert account_health(work)["queue_health"] is None
        only_queue = receipt()
        del only_queue["retry_health"]
        update_health(personal, only_queue)
        assert account_health(personal)["retry_health"] is None
        assert account_health(personal)["queue_health"] is not None
        assert account_health(work)["retry_health"] is not None


def test_separately_sampled_counts_do_not_reject_or_claim_idle(application):
    with session(application) as db:
        account = db.get(Account, "personal")
        # Each snapshot can be sampled between worker mutations.
        update_health(account, receipt() | {"pending_count": 0})
        assert account_health(account)["health"] == "backlog"
        update_health(account, FULL | {"retry_health": receipt()["retry_health"]})
        assert account_health(account)["health"] == "backlog"


def test_retained_unsupported_is_not_pending_or_completion(application):
    with session(application) as db:
        account = db.get(Account, "personal")
        queue = receipt()["queue_health"]
        queue.update({key: lane() for key in health.QUEUE_LANES})
        queue["last_successful_processing_at"] = None
        queue["completed_since_start"] = {"text": 0, "media": 0}
        update_health(account, FULL | {"queue_health": queue})
        result = account_health(account)
        assert result["health"] == "healthy"
        assert result["queue_health"]["retained_unsupported_count"] == 7
        assert result["queue_health"]["completed_since_start"] == {"text": 0, "media": 0}
        assert result["queue_health"]["last_successful_processing_at"] is None


def test_bootstrap_adds_snapshot_table_without_altering_existing_health(application):
    with session(application) as db:
        update_health(db.get(Account, "personal"), FULL)
    engine = application.extensions["db_engine"]
    def columns():
        return [{**column, "type": str(column["type"])} for column in inspect(engine).get_columns("account_health")]
    before = columns()
    AccountQueueHealth.__table__.drop(engine)
    assert "account_queue_health" not in inspect(engine).get_table_names()
    initialize(application.config)
    initialize(application.config)
    assert columns() == before
    with session(application) as db:
        account = db.get(Account, "personal")
        assert account_health(account)["health"] == "healthy"
        assert account_health(account)["queue_health"] is None
        update_health(account, receipt())
        assert account_health(account)["queue_health"] is not None


def test_internal_receipt_retry_exposes_snapshot_without_requiring_status_change(application, client, internal):
    payload = receipt() | {"account_id": "personal", "status": "connected"}
    for _ in range(2):
        response = client.post("/internal/accounts/status", json=payload, headers=internal, base_url=BASE)
        assert response.status_code == 200, response.get_json()
    with session(application) as db:
        result = account_health(db.get(Account, "personal"))
        assert result["retry_health"]["failed"] == 2
        assert result["queue_health"]["completed_since_start"]["text"] == 8


def test_snapshot_allows_only_current_safe_reason_codes(application):
    with session(application) as db:
        payload = receipt()
        payload["retry_health"]["errors_by_code"] = {reason: 1 for reason in health.RETRY_REASONS}
        update_health(db.get(Account, "personal"), payload)
        assert len(account_health(db.get(Account, "personal"))["retry_health"]["errors_by_code"]) == len(health.RETRY_REASONS)
