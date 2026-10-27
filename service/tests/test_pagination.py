"""Synthetic keyset traversal and local-only alias API integration."""
import json
from types import SimpleNamespace

import pytest
from flask import g
from itsdangerous import TimestampSigner
from sqlalchemy import select
from werkzeug.exceptions import Forbidden

from app.api import tool, tool_catalog
from app.models import Account, AccountHealth, Event, Message, Profile
from app.pagination import (CURSOR_MAX_AGE, _serializer, cursor_context,
                            decode_cursor, normalized_timestamp)
from test_api import payload, rpc
from test_auth import BASE, authorize, exchange

STAMP = normalized_timestamp("2026-10-26T12:00:00Z")
OLDER = normalized_timestamp("2026-10-25T12:00:00Z")
PN = "60123456789@s.whatsapp.net"


@pytest.fixture
def archive(application):
    with application.test_request_context():
        g.db = application.extensions["db_session"]()
        try:
            yield g.db
            g.db.commit()
        finally:
            g.db.close()


def add(db, count=1, chat=PN, timestamp=STAMP, account="personal", prefix="m", text="search fixture", chat_name=""):
    rows = [Message(id=f"{prefix}-{index:04d}", message_id=f"{prefix}-{index:04d}", account_id=account,
                    chat_jid=chat, sender_jid=PN, timestamp=timestamp, text=text, chat_name=chat_name,
                    sender_name="Observed sender") for index in range(count)]
    db.add_all(rows)
    db.flush()
    return rows


def search(**args):
    return tool("search_messages", {"account_id": "personal", **args}, "owner")


def test_more_than_100_equal_timestamp_messages_have_no_gaps_and_limit_can_change(archive):
    expected = add(archive, 235)
    add(archive, 2, timestamp=OLDER, prefix="old")
    first = search(limit=100)
    assert len(first["items"]) == 100 and first["has_more"] and first["next_cursor"]
    second = search(limit=80, cursor=first["next_cursor"])
    third = search(limit=100, cursor=second["next_cursor"])
    actual = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert actual == [item.id for item in expected] + ["old-0000", "old-0001"]
    assert len(set(actual)) == 237
    assert third["has_more"] is False and third["next_cursor"] is None


def test_exact_last_page_and_empty_archive(archive):
    assert search() == {"items": [], "next_cursor": None, "has_more": False}
    add(archive, 6)
    first = search(limit=3)
    last = search(limit=3, cursor=first["next_cursor"])
    assert len(last["items"]) == 3 and last["has_more"] is False and last["next_cursor"] is None
    assert search(query="absent") == {"items": [], "next_cursor": None, "has_more": False}


def test_strict_before_query_chat_and_cursor_filters(archive):
    add(archive, 3, timestamp=OLDER, text="literal % query", prefix="matching")
    add(archive, timestamp=STAMP, text="literal % query", prefix="too-new")
    add(archive, timestamp=OLDER, text="different", prefix="wrong-text")
    add(archive, timestamp=OLDER, text="literal % query", chat="other@g.us", prefix="wrong-chat")
    filters = {"query": "%", "chat_jid": PN, "before": STAMP, "limit": 1}
    first = search(**filters)
    assert first["items"][0]["id"] == "matching-0000"
    cursor = first["next_cursor"]
    second = search(**{**filters, "cursor": cursor, "before": "2026-10-26T20:00:00+08:00"})
    assert second["items"][0]["id"] == "matching-0001"
    for changed in ({"query": "different"}, {"chat_jid": "other@g.us"}, {"before": "2026-10-27T00:00:00Z"}):
        with pytest.raises(ValueError, match="^Invalid or expired cursor$"):
            search(**{**filters, **changed, "cursor": cursor})
    with pytest.raises(ValueError, match="^Invalid or expired cursor$"):
        tool("list_chats", {"account_id": "personal", "cursor": cursor}, "owner")
    payload = _serializer().loads(cursor)
    assert set(payload) == {"v", "c", "p"} and len(payload["c"]) == 64
    assert "query" not in json.dumps(payload)


def test_cursor_account_owner_tamper_expiry_and_payload_validation(application, archive, monkeypatch):
    add(archive, 3)
    application.config["ACCOUNTS"].append({"id": "work", "label": "Work"})
    archive.add(Account(id="work", owner_id="owner", label="Work"))
    archive.add(Profile(account_id="work", creation_key="work"))
    archive.flush()
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: 100000)
    cursor = search(limit=1)["next_cursor"]
    context = cursor_context("owner", "personal", "search_messages", {"query": "", "chat_jid": "", "before": None})
    assert decode_cursor(cursor, context)["id"] == "m-0000"
    with pytest.raises(ValueError, match="^Invalid or expired cursor$"):
        tool("search_messages", {"account_id": "work", "cursor": cursor}, "owner")
    with pytest.raises(ValueError, match="^Invalid or expired cursor$"):
        decode_cursor(cursor, cursor_context("other-owner", "personal", "search_messages", {"query": "", "chat_jid": "", "before": None}))
    with pytest.raises(ValueError, match="Unknown account"):
        tool("search_messages", {"account_id": "personal", "cursor": cursor}, "other-owner")
    for invalid in ("", "x" * 4097, "not-a-cursor", cursor[:10]+"!"+cursor[11:], None, 1, False, [], {}):
        with pytest.raises(ValueError, match="^Invalid or expired cursor$"):
            decode_cursor(invalid, context)
    valid = _serializer().loads(cursor)
    for bad in ([], {**valid, "v": True}, {**valid, "v": 2}, {**valid, "extra": 1},
                {**valid, "c": None}, {**valid, "p": []}, {**valid, "p": {"timestamp": STAMP, "id": ""}},
                {**valid, "p": {"timestamp": "bad-private-content", "id": "m"}},
                {**valid, "p": {"timestamp": STAMP, "id": "m", "extra": 1}}):
        with pytest.raises(ValueError, match="^Invalid or expired cursor$"):
            decode_cursor(_serializer().dumps(bad), context)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: 100000+CURSOR_MAX_AGE+1)
    with pytest.raises(ValueError, match="^Invalid or expired cursor$"):
        decode_cursor(cursor, context)


@pytest.mark.parametrize("args", [{"query": 0}, {"query": None}, {"query": False}, {"query": "x"*1001},
                                {"chat_jid": []}, {"chat_jid": "x"*301}, {"before": None}, {"before": ""},
                                {"before": "2026-10-26"}, {"before": 0}, {"cursor": None},
                                {"limit": True}, {"limit": 0}, {"limit": 101}, {"limit": "1"}, {"surprise": "x"}])
def test_search_rejects_invalid_optional_arguments(archive, args):
    with pytest.raises(ValueError):
        search(**args)


def test_equal_latest_chat_timestamps_and_latest_observed_name(archive):
    for index in range(105):
        jid = f"chat-{index:03d}@g.us"
        add(archive, chat=jid, prefix=f"chat-{index:03d}", chat_name="New name")
        add(archive, chat=jid, timestamp=OLDER, prefix=f"old-{index:03d}", chat_name="Z old name")
    first = tool("list_chats", {"account_id": "personal", "limit": 100}, "owner")
    last = tool("list_chats", {"account_id": "personal", "limit": 100, "cursor": first["next_cursor"]}, "owner")
    items = first["items"] + last["items"]
    assert [item["chat_jid"] for item in items] == [f"chat-{index:03d}@g.us" for index in range(105)]
    assert all(item["chat_name"] == "New name" and item["observed_chat_name"] == "New name" and item["message_count"] == 2 for item in items)
    assert last["has_more"] is False and last["next_cursor"] is None


def test_tool_catalog_and_dispatch_remain_read_only_even_for_old_write_claims(archive):
    add(archive)
    for scope in ("whatsapp:read", "whatsapp:read archive:aliases:write", "whatsapp:send"):
        token = SimpleNamespace(scope=scope)
        assert len(tool_catalog(token)) == 7
        assert all(spec["annotations"]["readOnlyHint"] for spec in tool_catalog(token))
        for name in ("set_alias", "remove_alias", "send_message", "mark_read", "delete_message", "send_reaction"):
            with pytest.raises(ValueError, match="Unknown tool"):
                tool(name, {"account_id": "personal"}, "owner", token)


def test_mcp_read_token_cannot_invoke_alias_mutations(client):
    token = exchange(client, authorize(client)).json["access_token"]
    for name in ("set_alias", "remove_alias"):
        args = {"account_id": "personal", "target_id": PN}
        if name == "set_alias":
            args["alias"] = "Local name"
        response = rpc(client, token, "tools/call", {"name": name, "arguments": args})
        assert response.status_code == 200 and response.json["error"]["code"] == -32602


def test_every_status_receipt_updates_health_without_repeating_event(client, application, internal, monkeypatch):
    report = {"account_id": "personal", "status": "connected", "whatsapp_connected": True,
              "whatsapp_authenticated": True, "pending_count": 0, "oldest_pending_at": None, "error": None}
    monkeypatch.setattr("app.health.now", lambda: 1000)
    assert client.post("/internal/accounts/status", base_url=BASE, headers=internal, json=report).status_code == 200
    monkeypatch.setattr("app.health.now", lambda: 1030)
    assert client.post("/internal/accounts/status", base_url=BASE, headers=internal, json=report).status_code == 200
    with application.extensions["db_session"]() as db:
        assert db.get(AccountHealth, "personal").last_bridge_seen_at == 1030
        assert len(list(db.scalars(select(Event).where(Event.event_type == "whatsapp.account.status")))) == 1
    token = exchange(client, authorize(client)).json["access_token"]
    response = rpc(client, token, "tools/call", {"name": "list_accounts"}).json
    account = json.loads(response["result"]["content"][0]["text"])[0]
    assert account["id"] == "personal" and account["status"] == "connected" and account["health"] == "healthy"
    assert account["last_bridge_seen_at"] == "1970-01-01T00:17:10Z"


def test_duplicate_ingestion_does_not_refresh_archive_freshness(client, application, internal, monkeypatch):
    monkeypatch.setattr("app.health.now", lambda: 1000)
    first = client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(timestamp=OLDER))
    assert first.status_code == 200 and first.json["duplicate"] is False
    monkeypatch.setattr("app.health.now", lambda: 1030)
    duplicate = client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(timestamp=OLDER))
    assert duplicate.status_code == 200 and duplicate.json["duplicate"] is True
    with application.extensions["db_session"]() as db:
        health = db.get(AccountHealth, "personal")
        assert health.last_message_ingested_at == 1000 and health.last_bridge_seen_at is None
        assert len(list(db.scalars(select(Event).where(Event.event_type == "whatsapp.message.received")))) == 1


def test_advertised_cursor_round_trip_through_authenticated_rpc(client, internal):
    token = exchange(client, authorize(client)).json["access_token"]
    advertised = rpc(client, token, "tools/list").json["result"]["tools"]
    schemas = {item["name"]: item["inputSchema"] for item in advertised}
    for name in ("search_messages", "list_chats", "search_contacts"):
        assert schemas[name]["properties"]["cursor"]["type"] == "string"
        assert "cursor" not in schemas[name]["required"]
    for index in range(3):
        body = payload(message_id=f"cursor-contract-{index}", timestamp=STAMP, text="contract fixture")
        assert client.post("/internal/messages", base_url=BASE, headers=internal, json=body).status_code == 200
    args = {"account_id": "personal", "query": "contract", "limit": 1}
    seen = []
    while True:
        response = rpc(client, token, "tools/call", {"name": "search_messages", "arguments": args}).json
        assert "error" not in response, response
        page = json.loads(response["result"]["content"][0]["text"])
        seen.extend(row["message_id"] for row in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"]
        args = {**args, "cursor": page["next_cursor"]}
    assert len(seen) == len(set(seen)) == 3
