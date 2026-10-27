"""Synthetic receive-only projections; no WhatsApp or private archive access."""
import hashlib
import json

import pytest
from sqlalchemy import inspect, select
from flask import g

from app.api import tool
from app.init_db import initialize
from app.models import Account, Event, Job, Message, MessageMetadata, MessageReaction, MessageState, MessageUpdate, Profile
from test_api import audio, payload, rpc
from test_auth import BASE, authorize, exchange


@pytest.fixture
def access(client):
    return exchange(client, authorize(client)).json["access_token"]


def ingest(client, internal, **changes):
    response = client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(**changes))
    assert response.status_code == 200, response.json
    return response.json["id"]


def edit_payload(**changes):
    return {"message_type": "text", "text": "edited text", "content": {}, "reply_to": None,
            "is_forwarded": False, "forwarding_score": 0, "content_truncated": False, **changes}


def update_payload(number=1, **changes):
    data = {"account_id": "personal", "event_id": hashlib.sha256(str(number).encode()).hexdigest(),
            "chat_jid": "chat@example", "target_message_id": "wa-test", "actor_jid": "actor@example",
            "actor_is_from_me": False, "kind": "edit", "timestamp": "2026-10-27T12:00:00Z",
            "edit": edit_payload()}
    if changes.get("kind") in ("reaction", "delete"):
        data.pop("edit")
    data.update(changes)
    return data


def update(client, internal, number=1, **changes):
    response = client.post("/internal/message-updates", base_url=BASE, headers=internal,
                           json=update_payload(number, **changes))
    assert response.status_code == 200, response.json
    return response.json


def call(client, access, name, **args):
    response = rpc(client, access, "tools/call", {"name": name, "arguments": {"account_id": "personal", **args}}).json
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


@pytest.mark.parametrize("kind,content", [
    ("text", {}), ("image", {}), ("video", {}), ("video_note", {}), ("audio", {}), ("document", {}), ("sticker", {}),
    ("contact", {"contacts": [{"name": "Synthetic", "vcard": "BEGIN:VCARD\nEND:VCARD"}]}),
    ("contacts", {"contacts": [{"name": "One"}, {"name": "Two"}]}),
    ("location", {"latitude": 3.1, "longitude": 101.6, "name": "Synthetic place", "address": "Fixture"}),
    ("live_location", {"latitude": -90, "longitude": 180}),
    ("poll", {"name": "Synthetic poll", "options": ["A", "B"], "selectable_count": 1}),
    ("interactive", {"kind": "button_reply", "text": "Option A", "selected_id": "a"}), ("unknown", {}),
])
def test_all_typed_messages(client, internal, access, kind, content):
    ident = ingest(client, internal, message_type=kind, content=content, content_truncated=True,
                   is_forwarded=True, forwarding_score=2)
    item = call(client, access, "get_message", id=ident)
    expected = content if kind != "unknown" else {"reason": "unrecognized_legacy_content", "recovery": "unknown"}
    assert item["message_type"] == kind and item["content"] == expected
    assert item["content_truncated"] and item["is_forwarded"] and item["forwarding_score"] == 2
    assert item["is_edited"] is item["is_deleted"] is False
    assert item["reactions"] == [] and item["reply_to"] is None


@pytest.mark.parametrize("changes", [
    {"message_type": "arbitrary"}, {"content": {"raw_proto": "private"}},
    {"message_type": "location", "content": {"latitude": 91}},
    {"message_type": "location", "content": {"latitude": 10**1000}},
    {"text": "invalid \ud800"},
    {"message_type": "location", "content": {"longitude": False}},
    {"message_type": "contacts", "content": {"contacts": [{}]*21}},
    {"message_type": "contact", "content": {"contacts": [{"vcard": "x"*8193}]}},
    {"message_type": "poll", "content": {"options": ["x"]*101}},
    {"message_type": "poll", "content": {"selectable_count": True}},
    {"message_type": "interactive", "content": {"selected_id": "x"*1001}},
    {"reply_to": {"message_id": "quoted", "chat_jid": "other"}},
    {"reply_to": {"message_id": "quoted", "raw": {}}}, {"text": "x"*65537},
    {"is_forwarded": 1}, {"forwarding_score": -1}, {"content_truncated": "true"},
])
def test_invalid_original_metadata_is_rejected(client, internal, application, changes):
    response = client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(**changes))
    assert response.status_code == 400
    with application.extensions["db_session"]() as db:
        assert db.scalar(select(Message)) is None


def test_legacy_rows_infer_type_and_bootstrap_is_additive(application):
    with application.extensions["db_session"].begin() as db:
        old = Message(**{k: v for k, v in payload().items() if k != "media"})
        db.add(old)
        db.flush()
        ident = old.id
    engine = application.extensions["db_engine"]
    columns = [col["name"] for col in inspect(engine).get_columns("messages")]
    for model in (MessageReaction, MessageUpdate, MessageState, MessageMetadata):
        model.__table__.drop(engine)
    initialize(application.config)
    assert [col["name"] for col in inspect(engine).get_columns("messages")] == columns
    with application.test_request_context():
        with application.extensions["db_session"]() as db:
            g.db = db
            item = tool("get_message", {"account_id": "personal", "id": ident}, "owner")
            assert item["message_type"] == "text" and item["text"] == "test hello"
            assert item["content_truncated"] is False
            assert db.get(Message, ident).text == "test hello"


def test_edits_dedupe_order_search_and_original_preserved(client, internal, access, application):
    ident = ingest(client, internal)
    assert not update(client, internal)["duplicate"]
    assert update(client, internal)["duplicate"]
    update(client, internal, 2, timestamp="2026-10-27T11:00:00Z", edit=edit_payload(text="outdated"))
    item = call(client, access, "get_message", id=ident)
    assert item["text"] == "edited text" and item["is_edited"]
    assert item["edited_at"] == "2026-10-27T12:00:00.000000Z"
    assert call(client, access, "search_messages", query="test hello")["items"] == []
    assert call(client, access, "search_messages", query="edited")["items"][0]["id"] == ident
    with application.extensions["db_session"]() as db:
        assert db.get(Message, ident).text == "test hello"
        assert len(list(db.scalars(select(MessageUpdate)))) == 2
        assert len(list(db.scalars(select(Event).where(Event.event_type == "whatsapp.message.received")))) == 1
        assert len(list(db.scalars(select(Event).where(Event.event_type == "whatsapp.message.updated")))) == 2


def test_equal_timestamp_tie_break_is_deterministic(client, internal, access):
    ident = ingest(client, internal)
    updates = sorted([update_payload(7), update_payload(8)], key=lambda x: x["event_id"])
    for data, value in ((updates[1], "winner"), (updates[0], "loser")):
        data["edit"]["text"] = value
        assert client.post("/internal/message-updates", base_url=BASE, headers=internal, json=data).status_code == 200
    assert call(client, access, "get_message", id=ident)["text"] == "winner"


def test_update_before_original_applies_and_emits_only_once(client, internal, access, application):
    update(client, internal, edit=edit_payload(text="arrived early", message_type="interactive",
                                   content={"kind": "button_reply", "text": "early"}, content_truncated=True))
    ident = ingest(client, internal, text="original late")
    ingest(client, internal, text="duplicate cannot overwrite")
    result = call(client, access, "get_message", id=ident)
    assert result["text"] == "arrived early" and result["message_type"] == "interactive"
    assert result["content"] == {"kind": "button_reply", "text": "early"} and result["content_truncated"]
    with application.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Event)))) == 2


def test_reactions_are_latest_per_actor_with_removal(client, internal, access):
    update(client, internal, 1, kind="reaction", reaction={"emoji": "👍"})
    ident = ingest(client, internal)
    update(client, internal, 2, kind="reaction", reaction={"emoji": "❤️"}, timestamp="2026-10-27T13:00:00Z")
    update(client, internal, 3, kind="reaction", reaction={"emoji": "old"}, timestamp="2026-10-27T10:00:00Z")
    update(client, internal, 4, kind="reaction", reaction={"emoji": "😂"}, actor_jid="second@example", actor_is_from_me=True)
    result = call(client, access, "get_message", id=ident)
    assert len(result["reactions"]) == 2 and result["reactions"][0]["emoji"] == "❤️"
    update(client, internal, 5, kind="reaction", reaction={"emoji": ""}, timestamp="2026-10-27T14:00:00Z")
    assert call(client, access, "get_message", id=ident)["reactions"] == [{
        "actor_jid": "second@example", "actor_is_from_me": True, "emoji": "😂", "timestamp": "2026-10-27T12:00:00.000000Z",
        "actor_name": "second@example", "actor_name_source": "jid", "actor_phone_number": None}]


def test_reactions_are_bounded(client, internal, access, monkeypatch):
    from app import message_updates
    monkeypatch.setattr(message_updates, "REACTION_LIMIT", 2)
    ident = ingest(client, internal)
    for i in range(3):
        update(client, internal, i, kind="reaction", reaction={"emoji": "👍"}, actor_jid=f"actor{i}@example")
    result = call(client, access, "get_message", id=ident)
    assert len(result["reactions"]) == 2 and result["reactions_truncated"] is True


def test_reply_resolution_fallback_truncation_and_scope(client, internal, access, application):
    target = ingest(client, internal, message_id="target", text="original target")
    ident = ingest(client, internal, message_id="reply", reply_to={"message_id": "target", "text": "stale quoted"})
    update(client, internal, target_message_id="target", edit=edit_payload(text="updated target"))
    quote = call(client, access, "get_message", id=ident)["reply_to"]
    assert quote["resolved"] and quote["id"] == target and quote["text"] == "updated target"
    missing = ingest(client, internal, message_id="missing-reply", reply_to={"message_id": "missing", "text": "x"*600})
    quote = call(client, access, "get_message", id=missing)["reply_to"]
    assert not quote["resolved"] and len(quote["text"]) == 512 and quote["text_truncated"]
    with application.extensions["db_session"].begin() as db:
        db.add(Account(id="other", label="Other", owner_id="owner")); db.flush()
        db.add(Profile(account_id="other", creation_key="other"))
        db.add(Message(account_id="other", message_id="missing", chat_jid="chat@example", sender_jid="private", timestamp="2026-10-27T12:00:00Z", text="CROSS ACCOUNT SECRET"))
    quote = call(client, access, "get_message", id=missing)["reply_to"]
    assert not quote["resolved"] and "SECRET" not in quote["text"]
    elsewhere = ingest(client, internal, message_id="elsewhere", chat_jid="different-chat", text="CROSS CHAT SECRET")
    scoped = ingest(client, internal, message_id="scoped-reply", reply_to={"message_id": "elsewhere"})
    assert call(client, access, "get_message", id=scoped)["reply_to"]["resolved"] is False


def test_delete_hides_text_media_transcript_quotes_and_cannot_resurrect(client, internal, access, application, monkeypatch):
    ident = ingest(client, internal, media=audio(), message_type="audio")
    reply = ingest(client, internal, message_id="reply", reply_to={"message_id": "wa-test", "text": "PRIVATE quoted original"})
    with application.extensions["db_session"].begin() as db:
        job = db.scalar(select(Job).where(Job.message_id == ident))
        job.state, job.result = "completed", json.dumps({"text": "PRIVATE transcript"})
    update(client, internal, kind="delete")
    update(client, internal, 2, timestamp="2026-10-28T00:00:00Z", edit=edit_payload(text="PRIVATE resurrection"))
    result = call(client, access, "get_message", id=ident)
    assert result["is_deleted"] and result["text"] == "" and result["content"] == {}
    assert result["media"] is result["transcription"] is result["reply_to"] is None
    assert "PRIVATE" not in json.dumps(result)
    assert call(client, access, "search_messages", query="PRIVATE")["items"] == []
    assert call(client, access, "search_messages", query="test hello")["items"][0]["id"] == reply
    quote = call(client, access, "get_message", id=reply)["reply_to"]
    assert quote["is_deleted"] and quote["text"] == "" and quote["message_type"] == "unknown"
    monkeypatch.setattr("app.storage.media_url", lambda media: pytest.fail("must not sign deleted media"))
    monkeypatch.setattr("app.storage.client", lambda: pytest.fail("must not read deleted image"))
    for name in ("get_media", "read_image"):
        response = rpc(client, access, "tools/call", {"name": name, "arguments": {"account_id": "personal", "id": ident}}).json
        assert response["error"]["code"] == -32602 and "deleted" in response["error"]["message"]
    with application.extensions["db_session"]() as db:
        message = db.get(Message, ident)
        assert message.text == "test hello" and message.media and db.scalar(select(Job)).result


def test_deleted_target_before_original_suppresses_quoted_fallback(client, internal, access):
    update(client, internal, kind="delete")
    reply = ingest(client, internal, message_id="reply", reply_to={"message_id": "wa-test", "text": "private"})
    quote = call(client, access, "get_message", id=reply)["reply_to"]
    assert quote["is_deleted"] and not quote["resolved"] and quote["text"] == ""
    ident = ingest(client, internal)
    assert call(client, access, "get_message", id=ident)["is_deleted"]
    assert call(client, access, "search_messages", query="test hello")["items"][0]["id"] == reply


def test_edit_before_original_replaces_stale_quoted_fallback(client, internal, access):
    update(client, internal, edit=edit_payload(text="fresh pending target", message_type="interactive",
        content={"kind": "list_reply", "text": "fresh"}))
    ident = ingest(client, internal, message_id="reply", reply_to={
        "message_id": "wa-test", "text": "stale private target", "message_type": "text"})
    quote = call(client, access, "get_message", id=ident)["reply_to"]
    assert not quote["resolved"] and quote["text"] == "fresh pending target"
    assert quote["message_type"] == "interactive"
    update(client, internal, 2, kind="delete")
    quote = call(client, access, "get_message", id=ident)["reply_to"]
    assert quote["is_deleted"] and quote["text"] == ""


def test_full_edit_snapshots_preserve_latest_typed_context(client, internal, access):
    ident = ingest(client, internal)
    newest = edit_payload(text="latest caption", message_type="location", content={
        "latitude": 1, "longitude": 2, "address": "a"*2000}, is_forwarded=True, forwarding_score=3)
    update(client, internal, 2, edit=newest, timestamp="2026-10-27T13:00:00Z")
    update(client, internal, 1, edit=edit_payload(text="old"))
    item = call(client, access, "get_message", id=ident)
    for key, value in newest.items():
        assert item[key] == value


@pytest.mark.parametrize("field", ["message_type", "text", "content", "reply_to", "is_forwarded", "forwarding_score", "content_truncated"])
def test_partial_edit_snapshots_are_rejected(client, internal, field):
    body = update_payload()
    body["edit"].pop(field)
    response = client.post("/internal/message-updates", base_url=BASE, headers=internal, json=body)
    assert response.status_code == 400


def test_event_dedupe_and_updates_are_account_scoped(client, internal, access, application):
    application.config["ACCOUNTS"] = application.config["ACCOUNTS"] + [{"id": "other", "label": "Other"}]
    with application.extensions["db_session"].begin() as db:
        db.add(Account(id="other", label="Other", owner_id="owner")); db.flush()
        db.add(Profile(account_id="other", creation_key="other"))
    ident = ingest(client, internal)
    other = ingest(client, internal, account_id="other", text="other original")
    update(client, internal, account_id="other")
    assert call(client, access, "get_message", id=ident)["text"] == "test hello"
    update(client, internal)
    assert call(client, access, "get_message", id=ident)["text"] == "edited text"
    assert call(client, access, "get_message", account_id="other", id=other)["text"] == "edited text"
    response = rpc(client, access, "tools/call", {"name": "get_message", "arguments": {"account_id": "personal", "id": other}}).json
    assert response["error"]["code"] == -32602


@pytest.mark.parametrize("changes", [
    {"kind": "send"}, {"event_id": "invalid"}, {"raw_proto": "private"}, {"actor_is_from_me": 1},
    {"timestamp": "2026-10-27T12:00:00+08:00"}, {"timestamp": "2026-10-27T12:00:00"},
    {"edit": {"message_type": "text"}}, {"edit": {"text": "ok", "media": {}}},
    {"reaction": {"emoji": "👍"}}, {"kind": "reaction", "reaction": {"emoji": "x"*65}},
    {"kind": "reaction", "reaction": {"emoji": "👍", "unknown": 1}},
    {"kind": "delete", "reaction": {"emoji": "👍"}}, {"account_id": "unknown"},
])
def test_invalid_updates_are_atomic(client, internal, application, changes):
    response = client.post("/internal/message-updates", base_url=BASE, headers=internal, json=update_payload(**changes))
    assert response.status_code == 400
    with application.extensions["db_session"]() as db:
        assert db.scalar(select(MessageUpdate)) is None and db.scalar(select(MessageState)) is None
        assert db.scalar(select(Event)) is None


def test_update_auth_and_receive_only_catalog(client, internal, access):
    assert client.post("/internal/message-updates", base_url=BASE, json=update_payload()).status_code == 401
    catalog = rpc(client, access, "tools/list").json["result"]["tools"]
    assert len(catalog) == 7 and "search_contacts" in {item["name"] for item in catalog}
    assert all(item["annotations"]["readOnlyHint"] for item in catalog)
    assert not any(any(word in item["name"] for word in ("send", "delete", "react", "edit")) for item in catalog)
    events = rpc(client, access, "events/list").json["result"]["events"]
    assert {"whatsapp.message.updated", "whatsapp.message.reaction"} <= {item["name"] for item in events}
    for path in ("/internal/send", "/internal/reactions", "/send", "/react"):
        assert client.post(path, base_url=BASE, headers=internal, json={}).status_code == 404


def test_format_ack_and_legacy_duplicate_metadata_enrichment(client, internal, access, application):
    with application.extensions["db_session"].begin() as db:
        original = Message(**payload())
        db.add(original); db.flush()
        ident = original.id
    response = client.post("/internal/messages", base_url=BASE, headers=internal,
                           json=payload(message_type="location", content={"latitude": 3, "longitude": 100}))
    assert response.status_code == 200 and response.json["duplicate"]
    assert response.headers["X-Archive-Format"] == "2"
    item = call(client, access, "get_message", id=ident)
    assert item["message_type"] == "location" and item["content"]["latitude"] == 3
    response = client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(text="overwritten"))
    assert response.headers["X-Archive-Format"] == "2"
    assert call(client, access, "get_message", id=ident)["text"] == "test hello"
    response = client.post("/internal/message-updates", base_url=BASE, headers=internal, json=update_payload())
    assert response.headers["X-Archive-Format"] == "2"
    response = client.post("/internal/message-updates", base_url=BASE, headers=internal, json=update_payload())
    assert response.headers["X-Archive-Format"] == "2" and response.json["duplicate"]
    with application.extensions["db_session"]() as db:
        assert not list(db.scalars(select(Event).where(Event.event_type == "whatsapp.message.received")))
