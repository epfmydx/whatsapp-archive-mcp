"""Synthetic known-contact search and actor enrichment; no phonebook access."""
import pytest
from sqlalchemy import event

from app.api import tool
from app.identities import identity_page, ingest_identities, remove_alias, set_alias
from app.message_updates import ingest_update, record_original
from app.models import Message, MessageMetadata, MessageReaction
from test_identities import (GROUP, LID, NEW, OLD, PN, metadata, link, message, second_account, session)
from test_message_updates import update_payload


def search(query, **kwargs):
    return tool("search_contacts", {"account_id": "personal", "query": query, **kwargs}, "owner")


def test_search_names_ids_phone_and_linked_aliases(application):
    with session(application) as db:
        message(db, sender=LID, chat=GROUP, sender_name="Observed person")
        ingest_identities(link())
        ingest_identities(metadata(name="Saved Person"))
        set_alias("personal", PN, "Friend 100%_exact")
        for query in ("saved PERSON", "friend", "100%_", "Observed", PN.upper(), "+60 (123) 456-789"):
            rows = search(query)["items"]
            assert {row["contact_id"] for row in rows} == {PN, LID}
            assert all(row["display_name"] == "Friend 100%_exact" and row["name_source"] == "alias" for row in rows)
            assert all(set(row["whatsapp_ids"]) == {PN, LID} and "phone_number" not in row for row in rows)
        assert search("100%_")["has_more"] is False
        assert search("100X_")["items"] == []
        assert search("person123")["items"] == []
        assert search("(123)", include_phone_number=True)["items"][0]["phone_number"] == "+60123456789"
        assert search("@g.us")["items"] == []
        assert search("998877@s.whatsapp.net")["items"] == []


def test_clear_and_bounded_history_match_resolver(application):
    from app.identities import HISTORICAL_CANDIDATE_LIMIT
    with session(application) as db:
        message(db, sender_name="Old Name")
        ingest_identities(link())
        ingest_identities(metadata(name="Old Saved", timestamp=OLD))
        ingest_identities(metadata(jid=LID, name="", timestamp=NEW))
        ingest_identities(metadata(jid=LID, name="", kind="push", timestamp=NEW))
        assert search("Old")["items"] == []
        for index in range(HISTORICAL_CANDIDATE_LIMIT+1):
            message(db, sender_name="Invalid\u202e", timestamp="2026-10-28T12:00:00Z", ident=str(index))
        assert search("Invalid")["items"] == []
        assert search("123456")["items"][0]["name_source"] == "phone"


def test_pagination_binding_isolation_and_invalid_inputs(application):
    with session(application) as db:
        for n in range(5):
            message(db, sender=f"actor-{n}@lid", chat=GROUP, ident=str(n), sender_name="Same name")
        first = search("same", limit=2)
        second = search("same", limit=2, cursor=first["next_cursor"])
        third = search("same", limit=2, cursor=second["next_cursor"])
        ids = [row["contact_id"] for page in (first, second, third) for row in page["items"]]
        assert ids == [f"actor-{n}@lid" for n in range(5)]
        assert first["has_more"] and second["has_more"] and not third["has_more"] and third["next_cursor"] is None
        for args in ({"query": "other"}, {"include_phone_number": True}, {"cursor": "tampered"}):
            supplied = {"query": "same", "cursor": first["next_cursor"], **args}
            with pytest.raises(ValueError, match="cursor"):
                search(**supplied)
        second_account(application, db)
        with pytest.raises(ValueError, match="cursor"):
            tool("search_contacts", {"account_id": "work", "query": "same", "cursor": first["next_cursor"]}, "owner")
        assert tool("search_contacts", {"account_id": "work", "query": "same"}, "owner")["items"] == []
        for args in ({"query": ""}, {"query": " "}, {"query": "x"*201}, {"query": 2}, {"limit": 0}, {"limit": True},
                     {"limit": 101}, {"include_phone_number": "false"}, {"include_phone_number": 0}):
            with pytest.raises(ValueError):
                search(**{"query": "same", **args})


def test_reply_and_reaction_candidates_no_bodies_or_shared_cards(application):
    with session(application) as db:
        item = message(db, chat=GROUP)
        record_original(item, {"message_type": "text", "text": "private body", "content": {"contacts": [{"name": "Unbound Card", "vcard": "99999"}]},
                               "reply_to": {"message_id": "unknown", "sender_jid": "reply@lid", "text": "private quote"}})
        ingest_update(update_payload(kind="reaction", chat_jid=GROUP, actor_jid="reaction@lid", target_message_id=item.message_id, reaction={"emoji": "👍"}))
        assert {"reply@lid", "reaction@lid", GROUP} <= set(identity_page("personal")["items"])
        set_alias("personal", "reply@lid", "Reply author")
        set_alias("personal", "reaction@lid", "Reaction author")
        statements, loaded = [], []
        event.listen(db, "do_orm_execute", lambda state: statements.append(str(state.statement.compile(dialect=db.bind.dialect))))
        event.listen(db, "loaded_as_persistent", lambda db, obj: loaded.append(obj))
        assert search("author")["has_more"] is False
        assert {item["contact_id"] for item in search("author")["items"]} == {"reaction@lid", "reply@lid"}
        assert search("Unbound Card")["items"] == []
        assert search("99999")["items"] == []
        assert not any(isinstance(obj, MessageMetadata) for obj in loaded)
        sql = " ".join(statements)
        assert "messages.text" not in sql and "messages.media" not in sql
        assert "json_extract" in sql


def test_actor_aliases_are_fresh_linked_and_account_scoped(application):
    with session(application) as db:
        target = message(db, ident="target", sender=LID)
        reply = message(db, ident="reply", sender=PN)
        record_original(reply, {"text": "hi", "reply_to": {"message_id": "target"}})
        ingest_identities(link())
        set_alias("personal", PN, "First alias")
        ingest_update(update_payload(kind="reaction", chat_jid=PN, target_message_id="reply", actor_jid=LID, reaction={"emoji": "👍"}))
        def read():
            return tool("get_message", {"account_id": "personal", "id": reply.id}, "owner")
        for name in ("First alias", "Renamed alias"):
            set_alias("personal", PN, name)
            result = read()
            assert result["sender_name"] == result["reply_to"]["sender_name"] == result["reactions"][0]["actor_name"] == name
            assert result["reply_to"]["sender_jid"] == LID and result["reactions"][0]["actor_jid"] == LID
            assert result["reply_to"]["sender_phone_number"] == result["reactions"][0]["actor_phone_number"] == "+60123456789"
        second_account(application, db)
        other = message(db, account="work", ident="other", sender=LID)
        record_original(other, {"text": "hi", "reply_to": {"message_id": "unknown", "sender_jid": LID}})
        isolated = tool("get_message", {"account_id": "work", "id": other.id}, "owner")
        assert isolated["reply_to"]["sender_name"] == LID and isolated["reply_to"]["sender_phone_number"] is None
        remove_alias("personal", PN)
        assert read()["reply_to"]["sender_name_source"] == "phone"
    with session(application):
        assert search("Renamed alias")["items"] == []


@pytest.mark.parametrize("sender", [None, "", "bad jid", "bad\u202e", 7])
def test_unresolved_legacy_reply_sender_does_not_break_message(application, sender):
    with session(application) as db:
        item = message(db)
        reply = {"message_id": "unknown", "sender_jid": sender}
        record_original(item, {"text": "hi", "reply_to": reply})
        result = tool("get_message", {"account_id": "personal", "id": item.id}, "owner")["reply_to"]
        assert not result["resolved"] and result["sender_jid"] == sender
        assert result["sender_name"] is None and result["sender_name_source"] == "unknown" and result["sender_phone_number"] is None


def test_deleted_quote_does_not_add_identity_fields(application):
    with session(application) as db:
        target = message(db, ident="target")
        quoted = message(db, ident="reply")
        record_original(quoted, {"text": "hi", "reply_to": {"message_id": target.message_id, "sender_jid": PN}})
        ingest_update(update_payload(kind="delete", chat_jid=PN, target_message_id=target.message_id))
        reply = tool("get_message", {"account_id": "personal", "id": quoted.id}, "owner")["reply_to"]
        assert reply["is_deleted"] and reply["text"] == ""
        assert not any(key.startswith("sender_") for key in reply)


def test_outgoing_only_direct_peer_name_is_searchable_and_shared_with_actors(application):
    owner = "owner@lid"
    with session(application) as db:
        original = message(db, chat=PN, sender=owner, outgoing=True, chat_name="Direct peer", sender_name="Account owner")
        quoted = message(db, chat=GROUP, sender=owner, ident="quote")
        record_original(quoted, {"text": "hi", "reply_to": {"message_id": "missing", "sender_jid": PN}})
        result = search("Direct peer")["items"]
        assert result == [{"contact_id": PN, "display_name": "Direct peer", "name_source": "observed", "whatsapp_ids": [PN]}]
        assert search("Account owner")["items"][0]["contact_id"] == owner
        value = tool("get_message", {"account_id": "personal", "id": quoted.id}, "owner")
        assert value["reply_to"]["sender_name"] == "Direct peer"
        assert value["reply_to"]["sender_name_source"] == "observed"
        set_alias("personal", PN, "Chat label", "chat")
        assert search("Chat label")["items"][0]["display_name"] == "Chat label"
        ingest_identities(metadata(name="Contact name"))
        assert search("Chat label")["items"][0]["display_name"] == "Contact name"
        set_alias("personal", PN, "Contact alias")
        assert search("Direct peer")["items"][0]["display_name"] == "Contact alias"
        value = tool("get_message", {"account_id": "personal", "id": original.id}, "owner")
        assert value["sender_name"] == "Account owner" and value["chat_name"] == "Chat label"


def test_chat_clear_suppresses_outgoing_peer_history(application):
    with session(application) as db:
        message(db, chat=PN, sender="owner@lid", outgoing=True, chat_name="Old peer")
        ingest_identities(metadata(jid=PN, name="", kind="chat", timestamp=NEW))
        assert search("Old peer")["items"] == []
        assert search(PN)["items"][0]["name_source"] == "phone"


def test_missing_reply_actor_and_malformed_reaction_keep_raw_ids(application):
    with session(application) as db:
        item = message(db)
        record_original(item, {"text": "hi", "reply_to": {"message_id": "unknown"}})
        ingest_update(update_payload(kind="reaction", chat_jid=PN, target_message_id=item.message_id,
                                     actor_jid="bad jid", reaction={"emoji": "👍"}))
        result = tool("get_message", {"account_id": "personal", "id": item.id}, "owner")
        assert "sender_jid" not in result["reply_to"]
        assert result["reply_to"]["sender_name_source"] == "unknown"
        assert result["reactions"][0]["actor_jid"] == "bad jid"
        assert result["reactions"][0]["actor_name"] is None
        assert result["reactions"][0]["actor_name_source"] == "unknown"
        assert "bad jid" not in identity_page("personal")["items"]


@pytest.mark.parametrize("control", ["\u061c", "\u200e", "\u202e", "\u2066", "\ufeff"])
def test_sql_identity_validation_excludes_unicode_format_characters(application, control):
    with session(application) as db:
        message(db, sender="bad"+control+"@lid", sender_name="Unsafe identity")
        assert search("Unsafe identity")["items"] == []
        assert all(control not in item for item in identity_page("personal")["items"])


def test_reaction_enrichment_query_count_is_constant_and_projects_bounded_metadata(application, monkeypatch):
    import app.message_updates as updates
    with session(application) as db:
        item = message(db, chat=GROUP)
        ident = item.id
        for n in range(101):
            db.add(MessageReaction(account_id="personal", chat_jid=GROUP, message_id=item.message_id,
                                   actor_jid=f"actor-{n}@lid", actor_is_from_me=False, emoji="👍",
                                   timestamp=NEW, event_id=str(n)))
    counts = []
    for limit in (1, 100):
        monkeypatch.setattr(updates, "REACTION_LIMIT", limit)
        with session(application) as db:
            item = db.get(Message, ident)
            statements = []
            event.listen(db, "do_orm_execute", lambda state: statements.append(state.statement))
            result = updates.message_view(item)
            assert len(result["reactions"]) == limit and result["reactions_truncated"]
            counts.append(len(statements))
            historical = [sql for sql in statements if "ORDER BY messages.timestamp" in str(sql)]
            assert len(historical) == 2
            for sql in historical:
                assert list(sql.compile().params.values()).count(16) == 1
                assert "row_number() OVER" in str(sql)
                assert "messages.text" not in str(sql) and "messages.media" not in str(sql)
    assert max(counts) <= 15 and counts[1] <= counts[0]+1, counts


def test_search_rendering_query_count_is_constant(application):
    with session(application) as db:
        for n in range(100):
            message(db, chat=GROUP, sender=f"actor-{n}@lid", sender_name="Same name", ident=str(n))
    counts = []
    for limit in (1, 100):
        with session(application) as db:
            statements = []
            event.listen(db, "do_orm_execute", lambda state: statements.append(state.statement))
            assert len(search("same name", limit=limit)["items"]) == limit
            counts.append(len(statements))
    assert max(counts) <= 15 and counts[1] <= counts[0]+1, counts


def test_current_push_and_chat_names_replace_historical_search_names(application):
    with session(application) as db:
        message(db, sender_name="Former sender", chat_name="Former chat")
        ingest_identities(metadata(kind="push", name="Current sender"))
        ingest_identities(metadata(kind="chat", name="Current chat"))
        assert search("Former")["items"] == []
        assert search("Current sender")["items"] and search("Current chat")["items"]


def test_batch_resolution_matches_scalar_for_links_aliases_clears_and_history(application):
    from flask import g
    from app.identities import actor_names, prefetch_actor_names
    with session(application) as db:
        message(db, sender=LID, chat=PN, sender_name="Older push", chat_name="Older chat")
        ingest_identities(link())
        ingest_identities(metadata(kind="push", name="", timestamp=NEW))
        message(db, sender=LID, chat=PN, sender_name="Restored push", timestamp="2026-10-28T12:00:00Z", ident="new")
        set_alias("personal", PN, "Linked alias")
        message(db, sender="unknown@lid", chat="direct@lid", chat_name="Only peer", ident="direct")
        roots = [PN, LID, "unknown@lid", "direct@lid", "absent@lid", GROUP]
        expected = {jid: actor_names("personal", jid, "actor") for jid in roots}
        g.identity_cache = {}
        prefetch_actor_names("personal", roots)
        assert {jid: actor_names("personal", jid, "actor") for jid in roots} == expected
        remove_alias("personal", PN)
        # after_flush invalidates every warmed entry; a fresh batch sees rename.
        ingest_identities(metadata(kind="contact_full", name="New name", timestamp="2026-10-29T00:00:00Z"))
        prefetch_actor_names("personal", roots)
        assert actor_names("personal", LID, "actor")["actor_name"] == "New name"


@pytest.mark.parametrize("query", ["ÉLODIE", "Élodie", "élodie"])
def test_unicode_case_insensitive_name_search(application, query):
    with session(application):
        ingest_identities(metadata(name="Élodie"))
        assert search(query)["items"][0]["display_name"] == "Élodie"


@pytest.mark.parametrize("with_original", [False, True])
def test_edit_reply_authors_and_update_actors_are_known_locally(application, with_original):
    from test_message_updates import edit_payload
    with session(application) as db:
        if with_original:
            message(db, ident="target", chat=GROUP)
        ingest_update(update_payload(kind="edit", actor_jid="editor@lid", chat_jid=GROUP, target_message_id="target",
            edit=edit_payload(reply_to={"message_id": "missing", "sender_jid": "edited-author@lid"})))
        assert {"editor@lid", "edited-author@lid"} <= set(identity_page("personal")["items"])
        set_alias("personal", "edited-author@lid", "Edited quoted author")
        assert search("Edited quoted author")["items"][0]["contact_id"] == "edited-author@lid"
