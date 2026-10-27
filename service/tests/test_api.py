import json
from sqlalchemy import select
from app.models import Job, Message, Event, Account
from app.auth import digest
from test_auth import BASE, authorize, exchange


def payload(**changes):
    data = {"account_id": "personal", "message_id": "wa-test", "chat_jid": "chat@example", "sender_jid": "sender@example", "timestamp": "2026-10-26T12:00:00Z", "text": "test hello", "is_from_me": False}
    data.update(changes)
    return data


def audio(**changes):
    data = {"object_key": "accounts/personal/media/uuid.ogg", "mime_type": "audio/ogg", "filename": "voice.ogg", "size": 20, "sha256": "a"*64}
    data.update(changes)
    return data


def rpc(client, access, method, params=None):
    return client.post("/mcp", base_url=BASE, headers={"Authorization": "Bearer "+access}, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_internal_auth_dedup_and_queue(client, internal, application):
    assert client.post("/internal/messages", base_url=BASE, json=payload()).status_code == 401
    first = client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(media=audio()))
    assert first.status_code == 200
    second = client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(media=audio()))
    assert first.json["id"] == second.json["id"] and second.json["duplicate"]
    with application.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(Job)))) == 1
        assert len(list(db.scalars(select(Event)))) == 1


def test_account_media_boundaries(client, internal):
    for body in (payload(account_id="other"), payload(media=audio(object_key="accounts/other/media/x")), payload(media=audio(object_key="accounts/personal/media/../secret"))):
        assert client.post("/internal/messages", base_url=BASE, headers=internal, json=body).status_code == 400


def test_worker_attempt_token_null_language_and_idempotency(client, internal, application):
    client.post("/internal/messages", base_url=BASE, headers=internal, json=payload(media=audio()))
    with application.extensions["db_session"].begin() as db:
        job = db.scalar(select(Job)); ident = job.id
        job.state, job.slot, job.attempt, job.attempt_token = "running", 1, 1, digest("attempt-token")
    url = "/internal/jobs/"+ident
    assert client.get(url, base_url=BASE, headers=internal).status_code == 403
    stale = internal | {"X-Job-Token": "old-attempt"}
    valid = internal | {"X-Job-Token": "attempt-token"}
    result = {"text": "real transcript fixture", "language": None, "segments": [], "engine": "test-engine", "model": "test-model"}
    assert client.post(url+"/complete", base_url=BASE, headers=stale, json=result).status_code == 403
    assert client.get(url, base_url=BASE, headers=valid).json["object_key"] == audio()["object_key"]
    assert client.post(url+"/complete", base_url=BASE, headers=valid, json=result).status_code == 200
    assert client.post(url+"/complete", base_url=BASE, headers=valid, json=result).status_code == 200
    with application.extensions["db_session"]() as db:
        job = db.get(Job, ident)
        assert job.slot == 1  # Capacity remains reserved until Kubernetes confirms terminal.
        assert json.loads(job.result)["language"] is None
        assert len(list(db.scalars(select(Event).where(Event.event_type == "whatsapp.transcription.completed")))) == 1


def test_mcp_discovery_read_only_and_owner_isolation(client, application, internal):
    token = exchange(client, authorize(client)).json["access_token"]
    discovery = rpc(client, token, "server/discover").json["result"]
    assert discovery == {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}, "events": {}}}
    tools = rpc(client, token, "tools/list").json["result"]["tools"]
    assert all(t["annotations"]["readOnlyHint"] for t in tools)
    assert not any(any(bad in t["name"] for bad in ("send", "delete", "react", "mark")) for t in tools)
    assert rpc(client, token, "tools/call", {"name": "send_message"}).json["error"]["code"] == -32602
    assert rpc(client, token, "tools/call", {"name": "search_messages", "arguments": {"account_id": "other"}}).json["error"]["code"] == -32602
    assert rpc(client, token, "events/list").json["result"]["events"][0]["delivery"] == ["webhook"]
    assert client.get("/mcp", base_url=BASE, headers={"Authorization": "Bearer "+token}).status_code == 405
    assert client.post("/mcp", base_url=BASE, headers={"Authorization": "Bearer "+token, "Origin": "https://evil.test"}, json={}).status_code == 403


def test_message_search_literal_wildcards(client, internal):
    token = exchange(client, authorize(client)).json["access_token"]
    client.post("/internal/messages", base_url=BASE, headers=internal, json=payload())
    data = rpc(client, token, "tools/call", {"name": "search_messages", "arguments": {"account_id": "personal", "query": "%"}}).json
    assert json.loads(data["result"]["content"][0]["text"]) == {"items": [], "next_cursor": None, "has_more": False}


def test_internal_hostname_and_untrusted_host(client, internal):
    response = client.post("/internal/messages", base_url="http://whatsapp-mcp:8000", headers=internal, json=payload())
    assert response.status_code == 200
    assert client.get("/healthz", base_url="https://evil.test").status_code == 400
