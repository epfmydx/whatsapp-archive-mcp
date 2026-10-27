"""Queue diagnostics on a disposable PostgreSQL schema, never a live database."""
from flask import g
from sqlalchemy import inspect

from app.health import account_health
from app.init_db import initialize
from app.models import Account, AccountHealth, AccountQueueHealth
from test_auth import BASE
from test_health import FULL
from test_health_snapshots import receipt
from test_registration_postgres import postgres_registration

HEADERS = {"Authorization": "Bearer " + "i" * 32}


def test_queue_side_table_bootstrap_preserves_existing_health(postgres_registration):
    application, config = postgres_registration
    engine = application.extensions["db_engine"]
    with application.extensions["db_session"].begin() as db:
        db.add(AccountHealth(account_id="personal", last_bridge_seen_at=123, pending_count=9))
    def columns():
        return [(col["name"], str(col["type"]), col["nullable"], col["default"])
                for col in inspect(engine).get_columns("account_health")]
    before = columns()
    AccountQueueHealth.__table__.drop(engine)
    initialize(config)
    initialize(config)
    assert columns() == before
    with application.extensions["db_session"]() as db:
        old = db.get(AccountHealth, "personal")
        assert old.last_bridge_seen_at == 123 and old.pending_count == 9
        assert db.get(AccountQueueHealth, "personal") is None


def test_postgres_receipt_snapshots_persist_and_legacy_omission_clears(postgres_registration):
    application, _ = postgres_registration
    with application.test_client() as client:
        body = {"account_id": "personal", "status": "connected", **receipt()}
        assert client.post("/internal/accounts/status", base_url=BASE, headers=HEADERS, json=body).status_code == 200
        with application.test_request_context():
            with application.extensions["db_session"]() as db:
                g.db = db
                health = account_health(db.get(Account, "personal"))
                assert health["queue_health"]["completed_since_start"] == {"text": 8, "media": 2}
                assert health["retry_health"]["errors_by_code"]["timeout"] == 1
        assert client.post("/internal/accounts/status", base_url=BASE, headers=HEADERS,
                           json={"account_id": "personal", "status": "connected", **FULL}).status_code == 200
        with application.extensions["db_session"]() as db:
            row = db.get(AccountQueueHealth, "personal")
            assert row.retry_health is None and row.queue_health is None
