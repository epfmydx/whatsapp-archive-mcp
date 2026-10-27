"""Synthetic public registration, owner approval, and durable revocation."""
import base64
import json
import pytest
from sqlalchemy import select, func, delete
from app import create_app
from app.auth import digest
from app.config import settings
from app.init_db import initialize
from app.models import Client, DynamicClient, Token, Code, Subscription, Event, Delivery, Owner, RateLimit, now
from app.registration import revoke_client, client_is_active, validate_redirect_uri
from app.events import deliver
from test_auth import BASE, VERIFIER, CHALLENGE, login, authorize, exchange


def register(client, **metadata):
    return client.post("/oauth/register", base_url=BASE, json={"client_name": "Desktop app", "redirect_uris": ["https://client.test/callback"]} | metadata)


def issue(client, cid):
    return exchange(client, authorize(client, client_id=cid), client_id=cid).json


def protected(client, access):
    return client.post("/mcp", base_url=BASE, headers={"Authorization": "Bearer " + access},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def refresh(client, cid, value):
    return client.post("/oauth/token", base_url=BASE, data={"grant_type": "refresh_token", "client_id": cid,
                       "refresh_token": value, "resource": BASE + "/mcp"})


def test_registration_is_persisted_public_profile_and_exact_consent(client, application):
    result = register(client, client_name="<script>alert(1)</script>")
    assert result.status_code == 201, result.data
    body = result.json
    assert body["client_id"].startswith("dcr_")
    assert body["token_endpoint_auth_method"] == "none"
    assert body["scope"] == "whatsapp:read"
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert "client_secret" not in body and "registration_access_token" not in body
    assert client.get("/.well-known/oauth-authorization-server", base_url=BASE).json["registration_endpoint"] == BASE + "/oauth/register"
    with application.extensions["db_session"]() as db:
        assert db.get(DynamicClient, body["client_id"]).created_at == body["client_id_issued_at"]
        assert client_is_active(db, body["client_id"], [])
        assert db.scalar(select(func.count()).select_from(Token)) == 0
    login(client)
    consent = client.get("/oauth/authorize", base_url=BASE, query_string={"client_id": body["client_id"], "response_type": "code", "resource": BASE+"/mcp", "redirect_uri": "https://client.test/callback", "code_challenge": CHALLENGE, "code_challenge_method": "S256"})
    assert consent.status_code == 200
    assert b"https://client.test/callback" in consent.data and b"unverified" in consent.data
    assert b"<script>" not in consent.data and b"&lt;script&gt;" in consent.data


def test_fixed_grant_profile_and_no_metadata_fetch(client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Registration must not fetch client metadata")
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    response = register(client, grant_types=["authorization_code"], client_uri="https://untrusted.test/", logo_uri="https://untrusted.test/logo", unknown_extension={"arbitrary": ["metadata"]})
    assert response.status_code == 201
    assert response.json["grant_types"] == ["authorization_code", "refresh_token"]


def test_generated_ids_never_overwrite_existing_client(client, monkeypatch):
    first = register(client).json["client_id"]
    choices = iter([first[4:], "replacement_generated_id"])
    monkeypatch.setattr("app.registration.secrets.token_urlsafe", lambda length: next(choices))
    second = register(client)
    assert second.status_code == 201
    assert second.json["client_id"] == "dcr_replacement_generated_id"


def test_dcr_full_pkce_refresh_mcp_survives_restart_and_bootstrap(client, application):
    cid = register(client).json["client_id"]
    assert authorize(client, client_id=cid, code_challenge_method="plain").status_code == 400
    assert authorize(client, client_id=cid, code_challenge="", code_challenge_method="").status_code == 400
    assert authorize(client, client_id=cid, redirect_uri="https://client.test/callback/extra").status_code == 400
    code = authorize(client, client_id=cid)
    assert exchange(client, code, client_id=cid, code_verifier="x"*64).status_code == 400
    issued = issue(client, cid)
    assert protected(client, issued["access_token"]).status_code == 200
    initialize(application.config)
    restarted = create_app(dict(application.config))
    try:
        new_client = restarted.test_client()
        assert protected(new_client, issued["access_token"]).status_code == 200
        rotated = refresh(new_client, cid, issued["refresh_token"])
        assert rotated.status_code == 200
        assert rotated.json["refresh_token"] != issued["refresh_token"]
        assert protected(new_client, rotated.json["access_token"]).status_code == 200
        assert refresh(new_client, cid, issued["refresh_token"]).status_code == 400
        assert protected(new_client, rotated.json["access_token"]).status_code == 401
    finally:
        restarted.extensions["db_engine"].dispose()


def test_disabled_policy_immediate_and_bootstrap_tombstone_never_resurrects(client, application):
    cid = register(client).json["client_id"]
    issued = issue(client, cid)
    pending = authorize(client, client_id=cid)
    application.config["OAUTH_DYNAMIC_REGISTRATION_ENABLED"] = False
    assert register(client).status_code == 404
    assert "registration_endpoint" not in client.get("/.well-known/oauth-authorization-server", base_url=BASE).json
    assert protected(client, issued["access_token"]).status_code == 401
    assert refresh(client, cid, issued["refresh_token"]).status_code == 400
    assert exchange(client, pending, client_id=cid).status_code == 400
    # Configured static clients keep working while dynamic registration is off.
    assert exchange(client, authorize(client)).status_code == 200
    initialize(application.config)
    application.config["OAUTH_DYNAMIC_REGISTRATION_ENABLED"] = True
    initialize(application.config)
    with application.extensions["db_session"]() as db:
        assert db.get(DynamicClient, cid)
        assert not db.get(Client, cid).enabled
        assert all(t.revoked for t in db.scalars(select(Token).where(Token.client_id == cid)))
    assert protected(client, issued["access_token"]).status_code == 401
    assert authorize(client, client_id=cid).status_code == 400


def test_revoke_helper_deletes_codes_revokes_families_disables_events(client, application):
    cid = register(client).json["client_id"]
    issued = issue(client, cid)
    authorize(client, client_id=cid)
    with application.extensions["db_session"].begin() as db:
        token = db.scalar(select(Token).where(Token.client_id == cid))
        db.add(Subscription(id="sub", client_id=cid, owner_id="owner", token_family=token.family, idempotency_key="sub", callback="https://callback.test", destination_ip="8.8.8.8", secret="encrypted", account_id="personal", event_type="whatsapp.message.received", expires_at=now()+900, active=True))
    with application.extensions["db_session"].begin() as db:
        assert revoke_client(db, cid)
        assert not revoke_client(db, "absent")
    initialize(application.config)
    with application.extensions["db_session"]() as db:
        assert db.get(DynamicClient, cid)
        assert not db.get(Client, cid).enabled
        assert list(db.scalars(select(Code).where(Code.client_id == cid))) == []
        assert all(t.revoked for t in db.scalars(select(Token).where(Token.client_id == cid)))
        assert not db.get(Subscription, "sub").active
    assert protected(client, issued["access_token"]).status_code == 401
    assert refresh(client, cid, issued["refresh_token"]).status_code == 400


@pytest.mark.parametrize("metadata", [
    {"redirect_uris": []}, {"redirect_uris": "https://client.test"}, {"redirect_uris": [None]},
    {"redirect_uris": ["https://client.test"]*2}, {"redirect_uris": ["https://client.test/"+str(i) for i in range(9)]},
    {"client_name": {}}, {"client_name": "x"*201}, {"client_name": "app\nname"},
    {"token_endpoint_auth_method": "client_secret_basic"}, {"token_endpoint_auth_method": None},
    {"scope": "whatsapp:read whatsapp:write"}, {"scope": ["whatsapp:read"]},
    {"grant_types": ["client_credentials"]}, {"grant_types": []}, {"grant_types": ["refresh_token"]},
    {"grant_types": ["authorization_code", {}]}, {"response_types": ["token"]},
    {"contacts": ["x"]*9}, {"client_uri": {}}, {"logo_uri": "x"*2049},
    {"jwks_uri": "https://metadata.test/keys"}, {"software_statement": "unsigned"},
])
def test_bad_metadata_is_rejected_without_registration(client, application, metadata):
    response = register(client, **metadata)
    assert response.status_code == 400 and response.json["error"] == "invalid_client_metadata"
    with application.extensions["db_session"]() as db:
        assert db.scalar(select(func.count()).select_from(DynamicClient)) == 0


@pytest.mark.parametrize("uri", [
    "http://evil.test/callback", "javascript:alert(1)", "https://*.test/callback", "https://test/path*",
    "https://user:pass@test/callback", "https://test/#fragment", "https://test/#", "https://test/\ncallback",
    "https://test/%0dcallback", "https://test/%1fcallback", "https://test/%5ccallback", "https://test/\\callback",
    "https://test:99999/callback", "https://test:0/callback", "https://test:bad/callback", "https://test:/callback",
    "https://test/%xx", "https://[::1]evil/callback", "https://bad_host/callback", "https://-bad.test/callback",
    "https://test./with space", "http://127.0.0.2/callback", "http://localhost.evil/callback", "https:///callback",
])
def test_unsafe_redirects_rejected(client, uri):
    assert register(client, redirect_uris=[uri]).status_code == 400


@pytest.mark.parametrize("uri", ["https://chatgpt.com/connector_platform/oauth/callback", "https://claude.ai/api/mcp/auth_callback", "http://localhost:3210/callback", "http://127.0.0.1:3210/callback", "http://[::1]:3210/callback", "https://client.test/callback?instance=one"])
def test_exact_https_and_loopback_callbacks(client, uri):
    response = register(client, redirect_uris=[uri], token_endpoint_auth_method="none")
    assert response.status_code == 201 and response.json["redirect_uris"] == [uri]


@pytest.mark.parametrize("raw,content_type,status", [(b'{"redirect_uris":[],"redirect_uris":[]}', "application/json", 400), (b'[]', "application/json", 400), (b'{', "application/json", 400), (b'{"unused":NaN}', "application/json", 400), (b'x'*8193, "application/json", 413), (b'{}', "text/plain", 415)])
def test_json_bounds_and_media_type(client, raw, content_type, status):
    assert client.post("/oauth/register", base_url=BASE, data=raw, content_type=content_type).status_code == status


def test_global_rate_budget_persists_and_fails_closed_without_owner(client, application):
    for _ in range(10):
        assert register(client, redirect_uris=[]).status_code == 400
    assert register(client).status_code == 429
    restarted = create_app(dict(application.config))
    try:
        assert register(restarted.test_client()).status_code == 429
    finally:
        restarted.extensions["db_engine"].dispose()
    with application.extensions["db_session"].begin() as db:
        db.execute(delete(Owner))
    assert register(client).status_code == 503


@pytest.mark.parametrize("live,pending", [(20, 20), (50, 0)])
def test_live_and_pending_quotas(client, application, live, pending):
    with application.extensions["db_session"].begin() as db:
        for index in range(live):
            cid = "dcr_fixture_"+str(index)
            db.add(Client(client_id=cid, name="Fixture", redirect_uris="https://client.test/callback"))
            db.flush()
            db.add(DynamicClient(client_id=cid))
            if index >= pending:
                db.add(Token(client_id=cid, owner_id="owner", access_hash=digest("a"+cid), refresh_hash=digest("r"+cid), family=cid, resource=BASE+"/mcp", scope="whatsapp:read", expires_at=now()+900, refresh_expires_at=now()+900))
    assert register(client).status_code == 429
    with application.extensions["db_session"].begin() as db:
        revoke_client(db, "dcr_fixture_0")
    assert register(client).status_code == 201


def test_https_required_and_static_namespace_reserved(client, application):
    assert client.post("/oauth/register", base_url="http://archive.test", headers={"X-Forwarded-Proto": "https"}, json={}).status_code == 400
    with pytest.raises(ValueError, match="reserved"):
        settings(dict(application.config, OAUTH_CLIENTS=[{"client_id": "dcr_abc", "name": "Spoof", "redirect_uris": ["https://client.test"]}]))
    with pytest.raises(ValueError, match="OAUTH_DYNAMIC"):
        settings(dict(application.config, OAUTH_DYNAMIC_REGISTRATION_ENABLED="maybe"))


@pytest.mark.parametrize("disabled", [False, True])
def test_outbox_allows_dynamic_clients_only_while_enabled(client, application, disabled):
    from cryptography.fernet import Fernet
    cid = register(client).json["client_id"]
    issue(client, cid)
    sessions = application.extensions["db_session"]
    secret = "whsec_"+base64.b64encode(b"s"*32).decode()
    with sessions.begin() as db:
        token = db.scalar(select(Token).where(Token.client_id == cid))
        db.add(Subscription(id="sub", client_id=cid, owner_id="owner", token_family=token.family, idempotency_key="sub", callback="https://callback.test/events", destination_ip="8.8.8.8", secret=Fernet(application.config["TOKEN_ENCRYPTION_KEY"].encode()).encrypt(secret.encode()).decode(), account_id="personal", event_type="whatsapp.message.received", expires_at=now()+900, active=True))
        db.add(Event(id="event", account_id="personal", event_type="whatsapp.message.received", payload='{"id":"fixture"}'))
        db.flush()
        db.add(Delivery(id="delivery", event_id="event", subscription_id="sub"))
    sent = []
    assert deliver(sessions, application.config["TOKEN_ENCRYPTION_KEY"], allowed_clients=[], dynamic_enabled=not disabled, post=lambda *args: (sent.append(args) or (200, b"{}")))
    assert bool(sent) is not disabled
