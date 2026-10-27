"""Archive metadata and paging checks on an opt-in disposable PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from flask import g
from sqlalchemy import select

from app.api import tool
from app.health import mark_ingested
from app.identities import ingest_identities, set_alias
from app.models import AccountHealth, IdentityAlias, IdentityName, Message
from test_registration_postgres import postgres_registration


def message(number):
    return Message(account_id="personal", message_id="archive-fixture-"+str(number),
                   chat_jid="12000000001@s.whatsapp.net", sender_jid="12000000001@s.whatsapp.net",
                   timestamp="2026-10-27T00:00:00.000000Z", text="Synthetic archive fixture")


def test_concurrent_message_ingestion_does_not_upgrade_fk_locks(postgres_registration):
    app, _ = postgres_registration
    barrier = Barrier(2)
    def insert(number):
        with app.test_request_context():
            with app.extensions["db_session"].begin() as db:
                g.db = db
                db.add(message(number))
                db.flush()  # Both transactions hold a PostgreSQL FK KEY SHARE lock.
                barrier.wait(timeout=10)
                mark_ingested("personal")
                return True
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(insert, (1, 2))) == [True, True]
    with app.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Message)))) == 2
        assert db.get(AccountHealth, "personal").last_message_ingested_at is not None


def test_equal_timestamp_cursor_pages_are_complete_on_postgresql(postgres_registration):
    app, _ = postgres_registration
    with app.extensions["db_session"].begin() as db:
        db.add_all(message(i) for i in range(123))
    with app.test_request_context():
        with app.extensions["db_session"]() as db:
            g.db = db
            args = {"account_id":"personal", "limit":50}
            ids = []
            while True:
                page = tool("search_messages", args, "owner")
                ids.extend(row["id"] for row in page["items"])
                if not page["has_more"]:
                    assert page["next_cursor"] is None
                    break
                args["cursor"] = page["next_cursor"]
            assert len(ids) == len(set(ids)) == 123
            chats = tool("list_chats", {"account_id":"personal"}, "owner")
            assert len(chats["items"]) == 1 and not chats["has_more"]


def test_concurrent_metadata_and_alias_upserts_preserve_one_row(postgres_registration):
    app, _ = postgres_registration
    with app.extensions["db_session"].begin() as db:
        db.add(message(1))
    barrier = Barrier(2)
    def update(number):
        with app.test_request_context():
            with app.extensions["db_session"].begin() as db:
                g.db = db
                barrier.wait(timeout=10)
                ingest_identities({"account_id":"personal", "names":[{
                    "jid":"12000000001@s.whatsapp.net", "kind":"contact_full", "name":"Synthetic "+str(number),
                    "observed_at":"2026-10-27T00:00:0"+str(number)+"Z"}]})
                set_alias("personal", "12000000001@s.whatsapp.net", "Local "+str(number))
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(update, (1, 2)))
    with app.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(IdentityAlias)))) == 1
        assert len(list(db.scalars(select(IdentityName)))) == 1
        assert db.scalar(select(IdentityName)).name == "Synthetic 2"
