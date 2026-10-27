import json
import uuid
from contextlib import contextmanager

import pytest
from flask import template_rendered
from sqlalchemy import select

from app.init_db import initialize
from app.models import Account, Base, Message, Owner, Profile, database
from test_api import payload, rpc
from test_auth import BASE, authorize, exchange, login


@pytest.fixture
def pool(application):
    application.config["ACCOUNTS"] = [
        {"id": "personal", "label": "Personal"},
        {"id": "number2", "label": "Available runtime", "profileSlot": True},
        {"id": "number3", "label": "Available runtime", "profileSlot": True},
    ]
    initialize(application.config)
    return application


@contextmanager
def rendered(application):
    captured = []

    def record(sender, template, context, **extra):
        captured.append(context)

    template_rendered.connect(record, application)
    try:
        yield captured
    finally:
        template_rendered.disconnect(record, application)


def csrf_token(client):
    with client.session_transaction(base_url=BASE) as session:
        token = session.get("csrf")
    if token is None:
        client.get("/profiles/new", base_url=BASE)
        with client.session_transaction(base_url=BASE) as session:
            token = session["csrf"]
    return token


def add_profile(client, name="Work", key=None, **extra):
    return client.post("/profiles", base_url=BASE, data={
        "csrf": csrf_token(client), "name": name,
        "creation_key": key or str(uuid.uuid4()), **extra})


def test_profile_owner_pages_and_forms_require_login_and_csrf(client, pool):
    for path in ("/", "/profiles/new", "/profiles/personal", "/accounts/personal/qr.png"):
        response = client.get(path, base_url=BASE)
        assert response.status_code == 302 and response.location == "/login"
    for path in ("/profiles", "/profiles/personal/rename"):
        assert client.post(path, base_url=BASE).status_code == 302
    login(client)
    assert client.get("/", base_url=BASE).status_code == 200
    assert client.get("/profiles/new", base_url=BASE).status_code == 200
    assert client.get("/profiles/personal", base_url=BASE).status_code == 200
    for path in ("/profiles", "/profiles/personal/rename"):
        assert client.post(path, base_url=BASE, data={"name": "Blocked"}).status_code == 400
        assert client.post(path, base_url=BASE, data={"name": "Blocked", "csrf": "wrong"}).status_code == 400
    with pool.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Profile)))) == 1
        assert db.get(Account, "personal").label == "Personal"


def test_create_chooses_runtime_and_replay_is_idempotent(client, pool):
    login(client)
    key = str(uuid.uuid4())
    response = add_profile(client, "  工作 📞  ", key, account_id="personal")
    assert response.status_code == 303 and response.location == "/profiles/number2"
    # An uncertain response may be retried with the original request UUID, even
    # after the owner edits its name. It must not allocate or rename again.
    renamed = client.post("/profiles/number2/rename", base_url=BASE,
                          data={"csrf": csrf_token(client), "name": "Renamed"})
    assert renamed.status_code == 303
    replay = add_profile(client, "A different name", key)
    assert replay.status_code == 303 and replay.location == response.location
    with pool.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Profile)))) == 2
        assert db.get(Profile, "number2").creation_key == key
        assert db.get(Account, "number2").label == "Renamed"
        assert db.get(Account, "personal").label == "Personal"


def test_capacity_is_friendly_and_duplicate_names_allowed(client, pool):
    login(client)
    assert add_profile(client, "Personal").status_code == 303
    assert add_profile(client, "Personal").status_code == 303
    with rendered(pool) as contexts:
        exhausted = add_profile(client, "More")
    assert exhausted.status_code == 409
    assert contexts[-1]["available_count"] == 0
    assert contexts[-1]["capacity"] == 3
    assert "administrator" in contexts[-1]["error"]
    with pool.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Profile)))) == 3


@pytest.mark.parametrize("name", ["", "   ", "x"*81, "line\nbreak", "tab\tname", "nul\x00name", "bidirectional\u202e", "delete\x7f"])
def test_invalid_names_rejected_on_create_and_rename(client, pool, name):
    login(client)
    assert add_profile(client, name).status_code == 400
    assert client.post("/profiles/personal/rename", base_url=BASE,
                       data={"csrf": csrf_token(client), "name": name}).status_code == 400
    with pool.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Profile)))) == 1
        assert db.get(Account, "personal").label == "Personal"


def test_names_are_escaped_and_limit_counts_unicode_characters(client, pool):
    login(client)
    malicious = '<script>alert("name")</script>'
    created = add_profile(client, malicious)
    assert created.status_code == 303
    for path in ("/", created.location):
        response = client.get(path, base_url=BASE)
        assert malicious.encode() not in response.data
        assert b"&lt;script&gt;" in response.data
    assert client.post(created.location+"/rename", base_url=BASE,
                       data={"csrf": csrf_token(client), "name": "名"*80}).status_code == 303
    # A rejected form must escape the submitted name in its input too.
    rejected = add_profile(client, malicious+"x"*81)
    assert rejected.status_code == 400
    assert malicious.encode() not in rejected.data


def test_creation_key_validation_preserves_capacity(client, pool):
    login(client)
    for value in ("", "not-a-uuid", "x"*300):
        assert client.post("/profiles", base_url=BASE, data={
            "csrf": csrf_token(client), "name": "Work", "creation_key": value}).status_code == 400
    with pool.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Profile)))) == 1


def test_unassigned_and_unconfigured_accounts_hidden_everywhere(client, pool, internal, monkeypatch):
    token = exchange(client, authorize(client)).json["access_token"]
    with rendered(pool) as contexts:
        assert client.get("/", base_url=BASE).status_code == 200
    assert [account.id for account in contexts[-1]["profiles"]] == ["personal"]
    assert contexts[-1]["available_count"] == 2

    def no_request(*args, **kwargs):
        raise AssertionError("Hidden accounts must not access a bridge or webhook")

    monkeypatch.setattr("app.profiles.requests.get", no_request)
    monkeypatch.setattr("app.events.public_destination", no_request)
    for hidden in ("number2", "unknown"):
        assert client.get("/profiles/"+hidden, base_url=BASE).status_code == 404
        assert client.get("/accounts/"+hidden+"/qr.png", base_url=BASE).status_code == 404
        assert client.post("/profiles/"+hidden+"/rename", base_url=BASE,
                           data={"csrf": csrf_token(client), "name": "Unknown"}).status_code == 404
        assert client.post("/internal/messages", base_url=BASE, headers=internal,
                           json=payload(account_id=hidden)).status_code == 400
        assert client.post("/internal/accounts/status", base_url=BASE, headers=internal,
                           json={"account_id": hidden, "status": "connected"}).status_code == 400
        result = rpc(client, token, "tools/call", {"name": "list_chats", "arguments": {"account_id": hidden}}).json
        assert result["error"]["code"] == -32602
        result = rpc(client, token, "events/subscribe", {"name": "whatsapp.message.received",
                     "arguments": {"account_id": hidden}, "delivery": {"mode": "webhook"}}).json
        assert result["error"]["code"] == -32602
    accounts = rpc(client, token, "tools/call", {"name": "list_accounts"}).json
    assert [account["id"] for account in json.loads(accounts["result"]["content"][0]["text"])] == ["personal"]
    # Even a durable assignment cannot bypass the current configured runtime set.
    pool.config["ACCOUNTS"] = pool.config["ACCOUNTS"][1:]
    assert client.get("/profiles/personal", base_url=BASE).status_code == 404
    assert client.get("/accounts/personal/qr.png", base_url=BASE).status_code == 404
    assert rpc(client, token, "events/subscribe", {"name": "whatsapp.message.received",
               "arguments": {"account_id": "personal"}}).json["error"]["code"] == -32602
    accounts = rpc(client, token, "tools/call", {"name": "list_accounts"}).json
    assert json.loads(accounts["result"]["content"][0]["text"]) == []


def test_other_owner_assignment_is_never_accessible_or_replayed(client, pool):
    key = str(uuid.uuid4())
    with pool.extensions["db_session"].begin() as db:
        db.add(Owner(id="other", email="other@example.test", password_hash="unused"))
        db.flush()
        db.get(Account, "number2").owner_id = "other"
        db.add(Profile(account_id="number2", creation_key=key))
    token = exchange(client, authorize(client)).json["access_token"]
    assert client.get("/profiles/number2", base_url=BASE).status_code == 404
    assert client.get("/accounts/number2/qr.png", base_url=BASE).status_code == 404
    assert add_profile(client, key=key).status_code == 409
    assert rpc(client, token, "tools/call", {"name": "list_chats", "arguments": {"account_id": "number2"}}).json["error"]["code"] == -32602
    assert rpc(client, token, "events/subscribe", {"name": "whatsapp.message.received", "arguments": {"account_id": "number2"}}).json["error"]["code"] == -32602
    assert add_profile(client).location == "/profiles/number3"


def test_additive_bootstrap_backfills_legacy_and_preserves_archive_and_names(application, tmp_path):
    c = dict(application.config, DATABASE_URL="sqlite:///"+str(tmp_path/"legacy.db"), ACCOUNTS=[
        {"id": "personal", "label": "Personal"}, {"id": "work", "label": "Work"},
        {"id": "number3", "label": "Runtime", "profileSlot": True}])
    engine, sessions = database(c["DATABASE_URL"])
    # Reproduce the previous schema without the newly introduced profile table.
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables if table.name != "profiles"])
    with sessions.begin() as db:
        db.add(Owner(id="owner", email=c["ADMIN_EMAIL"], password_hash="existing-password"))
        db.flush()
        db.add(Account(id="personal", owner_id="owner", label="My existing name", status="connected"))
        db.flush()
        db.add(Message(id="existing", account_id="personal", message_id="old", chat_jid="chat", sender_jid="sender", timestamp="2026-10-26T00:00:00Z", text="archive fixture"))
    initialize(c)
    with sessions.begin() as db:
        assert {p.account_id for p in db.scalars(select(Profile))} == {"personal", "work"}
        account = db.get(Account, "personal")
        assert account.label == "My existing name" and account.status == "connected"
        assert db.get(Message, "existing").text == "archive fixture"
        assert db.get(Owner, "owner").password_hash == "existing-password"
        account.label = "Renamed legacy"
        db.get(Account, "number3").label = "Named slot"
        db.add(Profile(account_id="number3", creation_key=str(uuid.uuid4())))
        legacy_key = db.get(Profile, "personal").creation_key
    initialize(c)
    with sessions() as db:
        assert db.get(Account, "personal").label == "Renamed legacy"
        assert db.get(Account, "number3").label == "Named slot"
        assert db.get(Profile, "personal").creation_key == legacy_key
        assert len(list(db.scalars(select(Profile)))) == 3
    engine.dispose()


def test_qr_only_reads_the_assigned_runtime(client, pool, monkeypatch):
    login(client)
    created = add_profile(client)
    pool.config["BRIDGE_URLS"] = {"number2": "http://configured-bridge:8080"}
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"status": "awaiting_pairing", "code": "synthetic-pairing-code"}

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("app.profiles.requests.get", get)
    response = client.get("/accounts/number2/qr.png", base_url=BASE)
    assert response.status_code == 200 and response.mimetype == "image/png"
    assert response.data.startswith(b"\x89PNG")
    assert calls == [("http://configured-bridge:8080/qr", {
        "headers": {"Authorization": "Bearer "+"i"*32}, "timeout": (3, 5), "allow_redirects": False})]
    for path in (created.location+"/delete", created.location+"/unlink", created.location+"/relink"):
        assert client.post(path, base_url=BASE, data={"csrf": csrf_token(client)}).status_code == 404
