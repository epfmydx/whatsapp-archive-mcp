"""Synthetic directory, archive and alias fixtures; no WhatsApp contacts."""
from contextlib import contextmanager

import pytest
from flask import g
from sqlalchemy import func, select

from app.identities import (identity_page, identity_phone, ingest_identities, install_identity_routes,
                            observed_timestamp, remove_alias, resolve_chat_name, resolve_identity, resolve_message_names, set_alias)
from app.models import Account, Event, IdentityLink, IdentityName, Job, Message, Profile
from test_auth import BASE

PN = "60123456789@s.whatsapp.net"
OTHER_PN = "60987654321@s.whatsapp.net"
LID = "123456789012345@lid"
GROUP = "123456789@g.us"
OLD = "2026-10-25T12:00:00Z"
NEW = "2026-10-26T12:00:00Z"
LATER = "2026-10-27T12:00:00Z"


@contextmanager
def session(application):
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        try:
            yield g.db
            g.db.commit()
        finally:
            g.db.close()


def message(db, chat=PN, sender=PN, chat_name="", sender_name="", timestamp=OLD, outgoing=False, account="personal", ident="one"):
    item = Message(account_id=account, message_id=ident, chat_jid=chat, sender_jid=sender,
                   chat_name=chat_name, sender_name=sender_name, timestamp=observed_timestamp(timestamp),
                   is_from_me=outgoing, text="synthetic")
    db.add(item)
    db.flush()
    return item


def metadata(jid=PN, name="Saved person", kind="contact_full", timestamp=NEW, account="personal"):
    return {"account_id": account, "names": [{"jid": jid, "kind": kind, "name": name, "observed_at": timestamp}]}


def link(pn=PN, timestamp=NEW, account="personal"):
    return {"account_id": account, "links": [{"lid": LID, "pn": pn, "observed_at": timestamp}]}


def second_account(application, db):
    application.config["ACCOUNTS"].append({"id": "work", "label": "Work"})
    db.add(Account(id="work", label="Work", owner_id="owner"))
    db.add(Profile(account_id="work", creation_key="work"))
    db.flush()


def test_read_time_enrichment_old_messages_alias_rename_remove(application):
    with session(application) as db:
        item = message(db, sender_name="Push before backfill")
        ingest_identities(metadata())
        assert resolve_message_names(item)["sender_name"] == "Saved person"
        assert item.sender_name == "Push before backfill"
        assert set_alias("personal", PN, "My local name") == {"name": "My local name", "source": "alias"}
        assert resolve_chat_name("personal", PN)["name"] == "My local name"
        assert set_alias("personal", PN, "Renamed")["name"] == "Renamed"
        assert remove_alias("personal", PN) == {"name": "Saved person", "source": "contact_full"}
        assert resolve_message_names(item)["sender_phone_number"] == "+60123456789"
        assert resolve_message_names(item)["chat_phone_number"] == "+60123456789"
        assert item.sender_jid == PN and item.chat_jid == PN
        assert db.scalar(select(func.count()).select_from(Message)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 0
        assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_account_isolation_known_targets_and_unassigned(application):
    with session(application) as db:
        second_account(application, db)
        ingest_identities(metadata(name="Personal"))
        ingest_identities(metadata(name="Work", account="work"))
        set_alias("work", PN, "Work alias")
        assert resolve_identity("personal", PN)["name"] == "Personal"
        assert resolve_identity("work", PN)["name"] == "Work alias"
        message(db, sender=OTHER_PN, chat=OTHER_PN, account="work")
        for operation in (lambda: set_alias("personal", OTHER_PN, "Leaked"),
                          lambda: remove_alias("personal", OTHER_PN),
                          lambda: resolve_identity("unknown", PN),
                          lambda: ingest_identities(metadata(account="unknown"))):
            with pytest.raises(ValueError):
                operation()
        db.delete(db.get(Profile, "work")); db.flush()
        with pytest.raises(ValueError):
            resolve_identity("work", PN)


def test_alias_chat_and_contact_are_distinct_and_known_directory_accepted(application):
    with session(application) as db:
        ingest_identities(metadata())
        assert set_alias("personal", PN, "Contact alias")["name"] == "Contact alias"
        assert set_alias("personal", PN, "Chat alias", "chat")["name"] == "Chat alias"
        assert resolve_identity("personal", PN)["name"] == "Contact alias"
        assert remove_alias("personal", PN, "chat")["name"] == "Contact alias"


def test_priority_newer_clear_and_older_replay(application):
    with session(application) as db:
        ingest_identities(metadata(name="Push", kind="push"))
        ingest_identities(metadata(name="Business", kind="business"))
        ingest_identities(metadata(name="First", kind="contact_first"))
        ingest_identities(metadata(name="Full", kind="contact_full"))
        assert resolve_identity("personal", PN) == {"name": "Full", "source": "contact_full"}
        ingest_identities(metadata(name="Older", timestamp=OLD))
        assert resolve_identity("personal", PN)["name"] == "Full"
        ingest_identities(metadata(name="", timestamp=LATER))
        ingest_identities(metadata(name="Replay", timestamp=NEW))
        row = db.get(IdentityName, ("personal", PN, "contact_full"))
        assert row.name == "" and row.observed_at == "2026-10-27T12:00:00.000000Z"
        assert resolve_identity("personal", PN) == {"name": "First", "source": "contact_first"}


def test_timestamp_normalization_and_equal_time_tie_is_deterministic(application):
    with session(application) as db:
        ingest_identities(metadata(name="Zulu", timestamp="2026-10-26T20:00:00+08:00"))
        ingest_identities(metadata(name="Alpha"))
        assert resolve_identity("personal", PN)["name"] == "Alpha"
        ingest_identities(metadata(name=""))
        ingest_identities(metadata(name="Zulu"))
        assert db.get(IdentityName, ("personal", PN, "contact_full")).name == ""


def test_explicit_lid_mapping_only_conflict_and_newer_replacement(application):
    with session(application) as db:
        ingest_identities(metadata(name="Phone person"))
        assert resolve_identity("personal", LID) == {"name": LID, "source": "jid"}
        assert identity_phone("personal", LID) is None
        ingest_identities(link())
        assert resolve_identity("personal", LID)["name"] == "Phone person"
        assert identity_phone("personal", LID) == "+60123456789"
        ingest_identities(link(OTHER_PN, OLD))
        assert identity_phone("personal", LID) == "+60123456789"
        ingest_identities(link(OTHER_PN))
        assert db.get(IdentityLink, ("personal", LID)).pn == ""
        assert identity_phone("personal", LID) is None
        assert resolve_identity("personal", LID)["source"] == "jid"
        ingest_identities(link())
        assert identity_phone("personal", LID) is None
        ingest_identities(link(OTHER_PN, LATER))
        assert identity_phone("personal", LID) == "+60987654321"
        assert resolve_identity("personal", LID) == {"name": "+60987654321", "source": "phone"}
        second_account(application, db)
        assert identity_phone("work", LID) is None


def test_phone_and_opaque_group_identifiers(application):
    with session(application):
        assert resolve_identity("personal", PN) == {"name": "+60123456789", "source": "phone"}
        assert resolve_identity("personal", LID) == {"name": LID, "source": "jid"}
        assert resolve_chat_name("personal", GROUP) == {"name": GROUP, "source": "jid"}
        assert identity_phone("personal", GROUP) is None
        assert identity_phone("personal", "123@other") is None


def test_latest_nonempty_history_not_lexical_max_and_outgoing_peer(application):
    with session(application) as db:
        message(db, sender_name="Zulu old", chat_name="Zulu old")
        message(db, sender_name="Alpha newer", chat_name="Alpha newer", timestamp=NEW, ident="two")
        latest = message(db, sender=OTHER_PN, sender_name="Owner", outgoing=True, timestamp=LATER, ident="three")
        names = resolve_message_names(latest)
        assert names["chat_name"] == "Alpha newer"
        assert names["sender_name"] == "Owner"
        assert resolve_identity("personal", PN)["name"] == "Alpha newer"
        # Group title and sender are separate identities, including self-sent messages.
        item = message(db, chat=GROUP, chat_name="Group title", sender=OTHER_PN, sender_name="Owner", outgoing=True, ident="group")
        assert resolve_message_names(item)["chat_name"] == "Group title"
        ingest_identities(metadata(jid=GROUP, kind="chat", name="New group title"))
        assert resolve_message_names(item)["chat_name"] == "New group title"


@pytest.mark.parametrize("bad", ["x"*201, "line\nbreak", "hidden\u202e", "nul\x00", 123, None])
def test_metadata_and_alias_reject_unsafe_names_atomically(application, bad):
    with session(application) as db:
        message(db)
        with pytest.raises(ValueError):
            set_alias("personal", PN, bad)
        body = metadata()
        body["names"].append({"jid": OTHER_PN, "kind": "push", "name": bad, "observed_at": NEW})
        with pytest.raises(ValueError):
            ingest_identities(body)
        assert db.scalar(select(func.count()).select_from(IdentityName)) == 0


@pytest.mark.parametrize("timestamp", ["no-date", "2026-10-26T12:00:00", "x"*51, 42, None])
def test_invalid_timestamp_rejected(application, timestamp):
    with session(application):
        with pytest.raises(ValueError):
            ingest_identities(metadata(timestamp=timestamp))


def test_directory_cursor_complete_distinct_and_scoped(application):
    with session(application) as db:
        second_account(application, db)
        message(db, chat=GROUP, sender=PN)
        message(db, chat=GROUP, sender=LID, ident="two")
        message(db, chat="private@other", sender="private@other", account="work", ident="work")
        ingest_identities(metadata(jid=OTHER_PN))
        pages, after = [], None
        while True:
            page = identity_page("personal", after, 1)
            pages += page["items"]
            after = page["next_after"]
            if not page["has_more"]:
                assert after is None
                break
        assert pages == sorted([GROUP, PN, LID, OTHER_PN])
        assert identity_page("personal", pages[-1], 100) == {"items": [], "next_after": None, "has_more": False}


def test_internal_routes_auth_validation_and_metadata_only(application, internal):
    # Root wires this installer during integration; support isolated module tests too.
    if "identities_ingest" not in application.view_functions:
        install_identity_routes(application)
    client = application.test_client()
    assert client.post("/internal/identities", base_url=BASE, json=metadata()).status_code == 401
    assert client.post("/internal/identities", base_url=BASE, headers=internal, json=metadata()).status_code == 200
    oversized = metadata(); oversized["names"] *= 101
    assert client.post("/internal/identities", base_url=BASE, headers=internal, json=oversized).status_code == 400
    response = client.get("/internal/accounts/personal/identities?limit=1", base_url=BASE, headers=internal)
    assert response.json == {"items": [PN], "has_more": False, "next_after": None}
    assert client.get("/internal/accounts/unknown/identities", base_url=BASE, headers=internal).status_code == 400
    assert client.get("/internal/accounts/personal/identities?limit=invalid", base_url=BASE, headers=internal).status_code == 400
    with session(application) as db:
        for model in (Message, Job, Event):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_clearing_push_and_chat_suppresses_older_archived_fallback(application):
    with session(application) as db:
        old = message(db, sender_name="Old push", chat=GROUP, chat_name="Old group")
        ingest_identities(metadata(name="", kind="push", timestamp=NEW))
        ingest_identities(metadata(jid=GROUP, name="", kind="chat", timestamp=NEW))
        names = resolve_message_names(old)
        assert names["sender_name"] == "+60123456789" and names["sender_name_source"] == "phone"
        assert names["chat_name"] == GROUP and names["chat_name_source"] == "jid"
        message(db, sender_name="New push", chat=GROUP, chat_name="New group", timestamp=LATER, ident="new")
        names = resolve_message_names(old)
        assert names["sender_name"] == "New push" and names["chat_name"] == "New group"


@pytest.mark.parametrize("jid", ["0123456789@s.whatsapp.net", "1234567890123456@s.whatsapp.net", "123@s.whatsapp.net"])
def test_invalid_phone_shapes_remain_opaque(application, jid):
    with session(application):
        assert identity_phone("personal", jid) is None
        assert resolve_identity("personal", jid) == {"name": jid, "source": "jid"}


def test_all_identity_and_health_writes_use_fk_compatible_account_lock(application):
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql
    from app.health import mark_ingested, update_health
    with session(application) as db:
        message(db)
        locks = []
        def capture(state):
            statement = state.statement
            if getattr(statement, "_for_update_arg", None) is not None:
                locks.append(str(statement.compile(dialect=postgresql.dialect())))
        event.listen(db, "do_orm_execute", capture)
        ingest_identities(metadata())
        set_alias("personal", PN, "Alias")
        remove_alias("personal", PN)
        update_health(db.get(Account, "personal"), {"error": None})
        mark_ingested("personal")
        assert len(locks) == 5
        assert all("FROM accounts" in sql and "FOR NO KEY UPDATE" in sql for sql in locks)
        assert all("FOR UPDATE" not in sql for sql in locks)


def test_metadata_write_refreshes_preloaded_name_and_mapping_after_lock(application):
    from sqlalchemy import update
    with session(application) as db:
        ingest_identities(metadata())
        ingest_identities(link())
        name = db.get(IdentityName, ("personal", PN, "contact_full"))
        mapping = db.get(IdentityLink, ("personal", LID))
        # Simulate committed state observed after waiting for the account lock,
        # while this Session still holds the older objects in its identity map.
        db.execute(update(IdentityName).where(IdentityName.account_id == "personal").values(
            name="Newer committed", observed_at="2026-10-27T12:00:00.000000Z").execution_options(synchronize_session=False))
        db.execute(update(IdentityLink).where(IdentityLink.account_id == "personal").values(
            pn=OTHER_PN, observed_at="2026-10-27T12:00:00.000000Z").execution_options(synchronize_session=False))
        assert name.name == "Saved person" and mapping.pn == PN
        ingest_identities(metadata(name="Stale replay"))
        ingest_identities(link())
        assert name.name == "Newer committed" and mapping.pn == OTHER_PN


def test_alias_lid_before_mapping_resolves_from_pn_then_manual_choice_is_canonical(application):
    from app.models import IdentityAlias
    with session(application) as db:
        message(db, sender=LID, chat=LID)
        set_alias("personal", LID, "Before phone mapping")
        ingest_identities(link())
        assert resolve_identity("personal", PN)["name"] == "Before phone mapping"
        assert resolve_identity("personal", LID)["name"] == "Before phone mapping"
        set_alias("personal", PN, "Latest manual choice")
        assert resolve_identity("personal", LID)["name"] == "Latest manual choice"
        assert db.get(IdentityAlias, ("personal", "contact", LID)) is None
        assert db.get(IdentityAlias, ("personal", "contact", PN)).alias == "Latest manual choice"
        set_alias("personal", LID, "Changed through linked ID")
        assert resolve_identity("personal", PN)["name"] == "Changed through linked ID"
        remove_alias("personal", LID)
        assert resolve_identity("personal", PN)["source"] != "alias"
        assert resolve_identity("personal", LID)["source"] != "alias"


def test_alias_pn_before_mapping_resolves_from_lid_and_conflict_does_not_leak(application):
    with session(application) as db:
        message(db)
        set_alias("personal", PN, "Phone alias")
        ingest_identities(link())
        assert resolve_identity("personal", LID)["name"] == "Phone alias"
        assert identity_phone("personal", PN) == "+60123456789"
        ingest_identities(link(OTHER_PN))
        assert resolve_identity("personal", LID) == {"name": LID, "source": "jid"}
        assert resolve_identity("personal", PN)["name"] == "Phone alias"
        second_account(application, db)
        ingest_identities(link(account="work"))
        assert resolve_identity("work", LID)["source"] == "phone"


def test_ambiguous_reverse_mapping_does_not_merge_lid_aliases(application):
    with session(application) as db:
        second_lid = "987654321012345@lid"
        message(db, sender=LID, chat=LID)
        set_alias("personal", LID, "One opaque identity")
        ingest_identities(link())
        other = link(); other["links"][0]["lid"] = second_lid
        ingest_identities(other)
        assert resolve_identity("personal", PN)["source"] != "alias"
        assert resolve_identity("personal", second_lid)["source"] != "alias"


def test_linked_chat_alias_shared_in_both_directions_and_contact_group_rejected(application):
    with session(application) as db:
        message(db, chat=LID, sender=LID)
        set_alias("personal", LID, "Chat before link", "chat")
        ingest_identities(link())
        assert resolve_chat_name("personal", PN)["name"] == "Chat before link"
        set_alias("personal", PN, "Chat after link", "chat")
        assert resolve_chat_name("personal", LID)["name"] == "Chat after link"
        remove_alias("personal", PN, "chat")
        assert resolve_chat_name("personal", LID)["source"] != "alias"
        message(db, chat=GROUP, ident="group")
        with pytest.raises(ValueError, match="chat alias"):
            set_alias("personal", GROUP, "Ineffective contact alias")
        assert set_alias("personal", GROUP, "Group alias", "chat")["name"] == "Group alias"


def test_new_alias_under_ambiguous_reverse_mapping_stays_on_exact_lid(application):
    with session(application) as db:
        second_lid = "987654321012345@lid"
        ingest_identities(link())
        other = link(); other["links"][0]["lid"] = second_lid
        ingest_identities(other)
        set_alias("personal", LID, "Exact local identity")
        assert resolve_identity("personal", LID)["name"] == "Exact local identity"
        assert resolve_identity("personal", second_lid)["source"] != "alias"
        assert resolve_identity("personal", PN)["source"] != "alias"
        assert identity_phone("personal", LID) == "+60123456789"
        assert identity_phone("personal", second_lid) == "+60123456789"


@pytest.mark.parametrize("older_id,clear_id", [(PN, LID), (LID, PN)])
@pytest.mark.parametrize("kind", ["contact_full", "push", "chat"])
def test_linked_clear_beats_older_alternate_metadata_and_stale_replay(application, older_id, clear_id, kind):
    with session(application):
        ingest_identities(link())
        ingest_identities(metadata(jid=older_id, name="Old linked name", kind=kind, timestamp=OLD))
        ingest_identities(metadata(jid=clear_id, name="", kind=kind, timestamp=NEW))
        resolver = resolve_chat_name if kind == "chat" else resolve_identity
        for jid in (PN, LID):
            assert resolver("personal", jid) == {"name": "+60123456789", "source": "phone"}
        # Replaying the old counterpart cannot remove the newer clear.
        ingest_identities(metadata(jid=older_id, name="Old replay", kind=kind, timestamp=OLD))
        for jid in (PN, LID):
            assert resolver("personal", jid)["source"] == "phone"
        ingest_identities(metadata(jid=older_id, name="Restored linked name", kind=kind, timestamp=LATER))
        for jid in (PN, LID):
            assert resolver("personal", jid) == {"name": "Restored linked name", "source": kind}


@pytest.mark.parametrize("older_id,clear_id", [(PN, LID), (LID, PN)])
@pytest.mark.parametrize("kind", ["push", "chat"])
def test_linked_clear_suppresses_alternate_older_history_but_new_history_restores(application, older_id, clear_id, kind):
    with session(application) as db:
        ingest_identities(link())
        old = message(db, chat=older_id, sender=older_id,
                      sender_name="Old push" if kind == "push" else "",
                      chat_name="Old chat" if kind == "chat" else "", timestamp=OLD)
        ingest_identities(metadata(jid=clear_id, name="", kind=kind, timestamp=NEW))
        resolver = resolve_chat_name if kind == "chat" else resolve_identity
        for jid in (PN, LID):
            assert resolver("personal", jid)["source"] == "phone"
        # Explicit fallback arguments from the old message cannot bypass it.
        old_names = resolve_message_names(old)
        assert old_names["sender_name" if kind == "push" else "chat_name"] == "+60123456789"
        message(db, chat=older_id, sender=older_id, ident="new",
                sender_name="New push" if kind == "push" else "",
                chat_name="New chat" if kind == "chat" else "", timestamp=LATER)
        for jid in (PN, LID):
            assert resolver("personal", jid) == {"name": "New push" if kind == "push" else "New chat", "source": "observed"}


@pytest.mark.parametrize("named_id,clear_id", [(PN, LID), (LID, PN)])
@pytest.mark.parametrize("kind", ["contact_full", "push", "chat"])
def test_linked_equal_time_clear_wins_independent_of_jid_direction(application, named_id, clear_id, kind):
    with session(application):
        ingest_identities(link())
        ingest_identities(metadata(jid=clear_id, name="", kind=kind, timestamp=NEW))
        ingest_identities(metadata(jid=named_id, name="Same-time old name", kind=kind, timestamp=NEW))
        resolver = resolve_chat_name if kind == "chat" else resolve_identity
        for jid in (PN, LID):
            assert resolver("personal", jid)["source"] == "phone"


def test_history_enrichment_projects_bounded_metadata_and_reuses_request_cache(application):
    from sqlalchemy import event, insert
    from app.identities import HISTORICAL_CANDIDATE_LIMIT
    with session(application) as db:
        db.execute(insert(Message), [{
            "id": "bulk-%04d" % index, "account_id": "personal", "message_id": "bulk-%04d" % index,
            "chat_jid": GROUP, "sender_jid": PN, "sender_name": "Synthetic sender",
            "chat_name": "Synthetic group", "timestamp": "2026-10-25T12:00:00.000000Z",
            "text": "x"*(64*1024), "is_from_me": False,
        } for index in range(1200)])
    with session(application) as db:
        loaded, queries = [], []
        event.listen(db, "loaded_as_persistent", lambda session, item: loaded.append(item) if isinstance(item, Message) else None)
        page = list(db.scalars(select(Message).order_by(Message.id).limit(100)))
        def capture(state):
            queries.append(state.statement)
        event.listen(db, "do_orm_execute", capture)
        for item in page:
            names = resolve_message_names(item)
            assert names["sender_name"] == "Synthetic sender"
            assert names["chat_name"] == "Synthetic group"
        assert len(loaded) == 100  # Enrichment loads no extra Message ORM objects.
        assert len(queries) <= 30  # Shared identities resolve once per request.
        historical = [statement for statement in queries
                      if "FROM messages" in str(statement) and "ORDER BY messages.timestamp" in str(statement)]
        assert len(historical) == 2
        for statement in historical:
            sql = str(statement)
            assert "messages.text" not in sql and "messages.media" not in sql
            assert statement._limit_clause.value == HISTORICAL_CANDIDATE_LIMIT
            assert len(list(statement.selected_columns)) == 3
    # A fresh request does not reuse the previous request's cached identities.
    with session(application) as db:
        loaded = []
        event.listen(db, "loaded_as_persistent", lambda session, item: loaded.append(item) if isinstance(item, Message) else None)
        item = db.get(Message, "bulk-0000")
        assert resolve_message_names(item)["sender_name"] == "Synthetic sender"
        assert len(loaded) == 1


def test_historical_invalid_candidate_window_is_bounded_and_clear_filter_precedes_limit(application):
    from sqlalchemy import event
    from app.identities import HISTORICAL_CANDIDATE_LIMIT
    with session(application) as db:
        message(db, sender_name="Older valid outside window", timestamp=OLD)
        for index in range(HISTORICAL_CANDIDATE_LIMIT+1):
            message(db, sender_name="Invalid\u202e", timestamp=NEW, ident="invalid-%02d" % index)
        assert resolve_identity("personal", PN)["source"] == "phone"
        ingest_identities(metadata(kind="push", name="", timestamp=NEW))
        statements = []
        event.listen(db, "do_orm_execute", lambda state: statements.append(state.statement))
        assert resolve_identity("personal", PN)["source"] == "phone"
        historical = [statement for statement in statements
                      if "FROM messages" in str(statement) and "ORDER BY messages.timestamp" in str(statement)]
        assert len(historical) == 1
        sql = str(historical[0])
        assert "messages.timestamp >" in sql and "LIMIT" in sql
        assert sql.index("messages.timestamp >") < sql.index("LIMIT")


def test_request_cache_invalidates_after_alias_metadata_and_archive_writes(application):
    with session(application) as db:
        message(db)
        assert resolve_identity("personal", PN)["source"] == "phone"
        ingest_identities(metadata(name="Directory"))
        assert resolve_identity("personal", PN)["name"] == "Directory"
        set_alias("personal", PN, "Alias")
        assert resolve_identity("personal", PN)["name"] == "Alias"
        remove_alias("personal", PN)
        assert resolve_identity("personal", PN)["name"] == "Directory"
        ingest_identities(metadata(name="", timestamp=LATER))
        assert resolve_identity("personal", PN)["source"] == "phone"
        message(db, sender_name="New archive observation", timestamp=LATER, ident="later")
        assert resolve_identity("personal", PN)["name"] == "New archive observation"


def test_get_message_keeps_raw_push_placeholder_but_displays_phone(application):
    from app.api import tool
    with session(application) as db:
        item = message(db, sender_name="-")
        result = tool("get_message", {"account_id": "personal", "id": item.id}, "owner")
        assert result["sender_name"] == "+60123456789"
        assert result["sender_name_source"] == "phone"
        assert result["observed_sender_name"] == "-"
        assert item.sender_name == "-"
        assert resolve_identity("personal", OTHER_PN, observed_name=" - ")["source"] == "phone"


def test_push_placeholders_do_not_hide_older_valid_history_or_exhaust_window(application):
    from app.identities import HISTORICAL_CANDIDATE_LIMIT
    with session(application) as db:
        message(db, sender_name="Known push", timestamp=OLD)
        for index in range(HISTORICAL_CANDIDATE_LIMIT+1):
            message(db, sender_name=" - ", timestamp=NEW, ident="placeholder-%02d" % index)
        assert resolve_identity("personal", PN) == {"name": "Known push", "source": "observed"}
        assert resolve_chat_name("personal", PN)["name"] == "Known push"


def test_push_placeholder_ingestion_is_ignored_without_clearing_directory(application):
    with session(application) as db:
        ingest_identities(metadata(kind="push", name="-"))
        assert db.get(IdentityName, ("personal", PN, "push")) is None
        assert resolve_identity("personal", PN)["source"] == "phone"
        ingest_identities(metadata(kind="push", name="Saved push", timestamp=OLD))
        ingest_identities(metadata(kind="push", name=" - ", timestamp=NEW))
        assert resolve_identity("personal", PN) == {"name": "Saved push", "source": "push"}
        assert db.get(IdentityName, ("personal", PN, "push")).observed_at == observed_timestamp(OLD)


def test_preexisting_directory_push_placeholder_is_not_a_display_candidate(application):
    with session(application) as db:
        db.add(IdentityName(account_id="personal", jid=PN, kind="push", name="-", observed_at=observed_timestamp(NEW)))
        db.flush()
        assert resolve_identity("personal", PN)["source"] == "phone"
        ingest_identities(link())
        ingest_identities(metadata(jid=LID, kind="push", name="Older linked push", timestamp=OLD))
        assert resolve_identity("personal", PN) == {"name": "Older linked push", "source": "push"}


def test_legitimate_hyphen_alias_contact_and_chat_names_are_preserved(application):
    with session(application) as db:
        message(db, chat=GROUP, chat_name="-", sender_name="-")
        assert resolve_chat_name("personal", GROUP) == {"name": "-", "source": "observed"}
        ingest_identities(metadata(jid=PN, name="-", kind="contact_full"))
        assert resolve_identity("personal", PN) == {"name": "-", "source": "contact_full"}
        ingest_identities(metadata(jid=GROUP, name="-", kind="chat"))
        assert resolve_chat_name("personal", GROUP) == {"name": "-", "source": "chat"}
        assert set_alias("personal", PN, "-") == {"name": "-", "source": "alias"}
        assert set_alias("personal", GROUP, "-", "chat") == {"name": "-", "source": "alias"}
