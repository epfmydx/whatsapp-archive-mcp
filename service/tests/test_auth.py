import base64
import hashlib
from urllib.parse import urlsplit, parse_qs
from sqlalchemy import select
from app.models import Token, Client, Code, Subscription, now

BASE = "https://archive.test"
VERIFIER = "v"*64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


def login(client):
    client.get("/login", base_url=BASE)
    with client.session_transaction(base_url=BASE) as session: csrf = session["csrf"]
    response = client.post("/login", base_url=BASE, data={"csrf": csrf, "email": "owner@example.invalid", "password": "a-test-password-123456"})
    assert response.status_code == 302


def authorize(client, **overrides):
    login(client)
    args = {"response_type": "code", "client_id": "desktop", "redirect_uri": "https://client.test/callback", "scope": "whatsapp:read", "resource": BASE+"/mcp", "state": "unchanged", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    args.update(overrides)
    response = client.get("/oauth/authorize", base_url=BASE, query_string=args)
    if response.status_code != 200: return response
    with client.session_transaction(base_url=BASE) as session: csrf = session["csrf"]
    response = client.post("/oauth/authorize", base_url=BASE, query_string=args, data={"csrf": csrf, "approve": "yes"})
    assert response.status_code == 302, response.data
    return parse_qs(urlsplit(response.location).query)["code"][0]


def exchange(client, code, **overrides):
    args = {"grant_type": "authorization_code", "client_id": "desktop", "code": code, "redirect_uri": "https://client.test/callback", "code_verifier": VERIFIER, "resource": BASE+"/mcp"}
    args.update(overrides)
    return client.post("/oauth/token", base_url=BASE, data=args)


def test_auth_code_is_one_use_and_hashes_at_rest(client, application):
    code = authorize(client)
    response = exchange(client, code)
    assert response.status_code == 200, response.data
    data = response.json
    assert data["scope"] == "whatsapp:read"
    assert exchange(client, code).status_code == 400
    with application.extensions["db_session"]() as db:
        token = db.scalar(select(Token))
        assert data["access_token"] not in token.access_hash
        assert data["refresh_token"] not in token.refresh_hash


def test_pkce_and_redirect_security(client):
    assert authorize(client, redirect_uri="https://evil.test/callback").status_code == 400
    assert authorize(client, code_challenge_method="plain").status_code == 400
    assert authorize(client, code_challenge_method="", code_challenge="").status_code == 400
    assert exchange(client, authorize(client), code_verifier="x"*64).status_code == 400


def test_resource_scope_and_password_grant_rejected(client):
    assert authorize(client, resource="https://evil.test").status_code == 400
    assert authorize(client, scope="whatsapp:write").status_code == 400
    assert client.post("/oauth/token", base_url=BASE, data={"grant_type": "password", "resource": BASE+"/mcp"}).status_code == 400


def test_refresh_rotation_replay_revokes_family(client):
    original = exchange(client, authorize(client)).json
    args = {"grant_type": "refresh_token", "client_id": "desktop", "refresh_token": original["refresh_token"], "resource": BASE+"/mcp"}
    new = client.post("/oauth/token", base_url=BASE, data=args)
    assert new.status_code == 200, new.data
    assert new.json["refresh_token"] != original["refresh_token"]
    assert client.post("/oauth/token", base_url=BASE, data=args).status_code == 400
    assert client.post("/mcp", base_url=BASE, headers={"Authorization": "Bearer "+new.json["access_token"]}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def test_revoke_and_csrf(client):
    client.get("/login", base_url=BASE)
    assert client.post("/login", base_url=BASE, data={"email": "owner@example.invalid", "password": "a-test-password-123456"}).status_code == 400
    data = exchange(client, authorize(client)).json
    assert client.post("/oauth/revoke", base_url=BASE, data={"token": data["refresh_token"], "client_id": "desktop"}).status_code == 200
    assert client.post("/mcp", base_url=BASE, headers={"Authorization": "Bearer "+data["access_token"]}, json={}).status_code == 401


def test_no_signup_or_open_return_url(client):
    assert client.post("/register", base_url=BASE).status_code == 404
    login(client)
    assert client.get("/login?next=https://evil.test", base_url=BASE).status_code == 200
    assert client.post("/logout", base_url=BASE).status_code == 400


def test_csrf_cannot_use_missing_session_default(client):
    assert client.post("/login", base_url=BASE, data={"csrf": "!", "email": "owner@example.invalid", "password": "a-test-password-123456"}).status_code == 400


def test_only_trusted_ingress_can_supply_https_scheme(application):
    from app import create_app
    config = dict(application.config)
    config["TRUSTED_PROXY_CIDRS"] = ["10.0.1.2/32"]
    proxy_app = create_app(config)
    client = proxy_app.test_client()
    code = authorize(client)
    args = {"grant_type": "authorization_code", "client_id": "desktop", "code": code, "redirect_uri": "https://client.test/callback", "code_verifier": VERIFIER, "resource": BASE+"/mcp"}
    untrusted = client.post("/oauth/token", base_url="http://archive.test", headers={"X-Forwarded-Proto": "https"}, environ_overrides={"REMOTE_ADDR": "10.0.1.3"}, data=args)
    assert untrusted.status_code == 400 and untrusted.json["error"] == "insecure_transport"
    trusted = client.post("/oauth/token", base_url="http://archive.test", headers={"X-Forwarded-Proto": "https"}, environ_overrides={"REMOTE_ADDR": "10.0.1.2"}, data=args)
    assert trusted.status_code == 200, trusted.data
    proxy_app.extensions["db_engine"].dispose()


def test_consent_csp_allows_only_validated_callback_origin(client, application):
    login(client)
    args = {"response_type": "code", "client_id": "desktop", "redirect_uri": "https://client.test/callback", "scope": "whatsapp:read", "resource": BASE+"/mcp", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    consent = client.get("/oauth/authorize", base_url=BASE, query_string=args)
    assert consent.status_code == 200
    assert "form-action 'self' https://client.test;" in consent.headers["Content-Security-Policy"]
    with client.session_transaction(base_url=BASE) as session: csrf = session["csrf"]
    callback = client.post("/oauth/authorize", base_url=BASE, query_string=args, data={"csrf": csrf, "approve": "yes"})
    assert callback.status_code == 302 and callback.location.startswith("https://client.test/callback?")
    assert "form-action 'self' https://client.test;" in callback.headers["Content-Security-Policy"]
    for evil in ("https://evil.test/callback", "https://*.evil.test/callback"):
        rejected = client.get("/oauth/authorize", base_url=BASE, query_string=args | {"redirect_uri": evil})
        assert rejected.status_code == 400
        assert "form-action 'self';" in rejected.headers["Content-Security-Policy"]
        assert "evil.test" not in rejected.headers["Content-Security-Policy"]
        rejected_post = client.post("/oauth/authorize", base_url=BASE, query_string=args | {"redirect_uri": evil}, data={"csrf": csrf, "approve": "yes"})
        assert rejected_post.status_code == 400
        assert "form-action 'self';" in rejected_post.headers["Content-Security-Policy"]
        assert "evil.test" not in rejected_post.headers["Content-Security-Policy"]
    ordinary = client.get("/login", base_url=BASE)
    assert "form-action 'self';" in ordinary.headers["Content-Security-Policy"]
    assert "client.test" not in ordinary.headers["Content-Security-Policy"]


def test_registered_wildcard_callback_cannot_broaden_csp(client, application):
    from app.models import Client
    with application.extensions["db_session"].begin() as db:
        db.get(Client, "desktop").redirect_uris = "https://*.evil.test/callback"
    rejected = authorize(client, redirect_uri="https://*.evil.test/callback")
    assert rejected.status_code == 400
    assert "form-action 'self';" in rejected.headers["Content-Security-Policy"]
    assert "evil.test" not in rejected.headers["Content-Security-Policy"]


def protected(client, access_token):
    return client.post("/mcp", base_url=BASE, headers={"Authorization": "Bearer "+access_token},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def refresh(client, refresh_token):
    return client.post("/oauth/token", base_url=BASE, data={"grant_type": "refresh_token", "client_id": "desktop",
                      "refresh_token": refresh_token, "resource": BASE+"/mcp"})


def test_current_allowlist_blocks_persisted_client_without_bootstrap(client, application):
    issued = exchange(client, authorize(client)).json
    outstanding = authorize(client)
    application.config["OAUTH_CLIENTS"] = []
    assert protected(client, issued["access_token"]).status_code == 401
    assert refresh(client, issued["refresh_token"]).status_code == 400
    assert exchange(client, outstanding).status_code == 400
    assert authorize(client).status_code == 400


def test_bootstrap_removal_revokes_grants_and_readd_requires_fresh_consent(client, application):
    from app.init_db import initialize
    issued = exchange(client, authorize(client)).json
    outstanding = authorize(client)
    with application.extensions["db_session"].begin() as db:
        token = db.scalar(select(Token))
        db.add(Subscription(id="old-sub", owner_id="owner", client_id="desktop", token_family=token.family,
                            idempotency_key="test", callback="https://client.test/events", destination_ip="8.8.8.8",
                            secret="encrypted", account_id="personal", event_type="message.created", expires_at=now()+900, active=True))
    original_clients = application.config["OAUTH_CLIENTS"]
    application.config["OAUTH_CLIENTS"] = []
    initialize(application.config)
    with application.extensions["db_session"]() as db:
        assert not db.get(Client, "desktop").enabled
        assert all(token.revoked for token in db.scalars(select(Token)))
        assert list(db.scalars(select(Code))) == []
        assert not db.get(Subscription, "old-sub").active
    assert protected(client, issued["access_token"]).status_code == 401
    assert refresh(client, issued["refresh_token"]).status_code == 400
    assert exchange(client, outstanding).status_code == 400
    application.config["OAUTH_CLIENTS"] = original_clients
    initialize(application.config)
    assert protected(client, issued["access_token"]).status_code == 401
    assert refresh(client, issued["refresh_token"]).status_code == 400
    assert exchange(client, outstanding).status_code == 400
    with application.extensions["db_session"]() as db:
        assert db.get(Client, "desktop").enabled
        assert not db.get(Subscription, "old-sub").active
    fresh = exchange(client, authorize(client))
    assert fresh.status_code == 200
    assert protected(client, fresh.json["access_token"]).status_code == 200


def test_bootstrap_reconciliation_rolls_back_on_invalid_client(client, application):
    import pytest
    from app.init_db import initialize
    issued = exchange(client, authorize(client)).json
    outstanding = authorize(client)
    bad = dict(application.config, OAUTH_CLIENTS=[{"client_id": "bad", "name": "Bad", "redirect_uris": []}])
    with pytest.raises(ValueError, match="exact redirect"):
        initialize(bad)
    with application.extensions["db_session"]() as db:
        assert db.get(Client, "desktop").enabled
        assert not db.scalar(select(Token)).revoked
        assert len(list(db.scalars(select(Code)))) == 1
    assert protected(client, issued["access_token"]).status_code == 200
    assert exchange(client, outstanding).status_code == 200
