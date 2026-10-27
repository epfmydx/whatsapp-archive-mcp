from sqlalchemy import select

from app.models import Client, Code, DynamicClient, Subscription, Token, now
from test_auth import BASE, authorize, exchange, login, protected, refresh


def test_connections_require_owner_and_csrf(client):
    assert client.get("/connections", base_url=BASE).location == "/login"
    assert client.post("/connections/desktop/disconnect", base_url=BASE).location == "/login"
    login(client)
    page = client.get("/connections", base_url=BASE)
    assert page.status_code == 200
    assert b"Manually configured" in page.data and b"https://client.test/callback" in page.data
    assert page.headers["Cache-Control"] == "no-store"
    assert client.post("/connections/desktop/disconnect", base_url=BASE).status_code == 400
    with client.session_transaction(base_url=BASE) as session:
        csrf = session["csrf"]
    assert client.post("/connections/missing/disconnect", base_url=BASE, data={"csrf": csrf}).status_code == 404


def test_owner_disconnect_revokes_all_access_and_events(client, application):
    issued = exchange(client, authorize(client)).json
    authorize(client)  # Also revoke an outstanding code.
    with application.extensions["db_session"].begin() as db:
        token = db.scalar(select(Token))
        db.add(Subscription(id="owner-disconnect", owner_id="owner", client_id="desktop", token_family=token.family,
                            idempotency_key="disconnect", callback="https://client.test/events", destination_ip="8.8.8.8",
                            secret="encrypted", account_id="personal", event_type="message.created",
                            expires_at=now()+900, active=True))
    with client.session_transaction(base_url=BASE) as session:
        csrf = session["csrf"]
    result = client.post("/connections/desktop/disconnect", base_url=BASE, data={"csrf": csrf})
    assert result.status_code == 303 and result.location == "/connections?disconnected=1"
    assert protected(client, issued["access_token"]).status_code == 401
    assert refresh(client, issued["refresh_token"]).status_code == 400
    with application.extensions["db_session"]() as db:
        assert not db.get(Client, "desktop").enabled
        assert all(token.revoked for token in db.scalars(select(Token)))
        assert list(db.scalars(select(Code))) == []
        assert not db.get(Subscription, "owner-disconnect").active
    page = client.get(result.location, base_url=BASE)
    assert b"Disconnected" in page.data and b"Client disconnected." in page.data


def test_connection_names_callbacks_escape_and_empty_state(client, application):
    with application.extensions["db_session"].begin() as db:
        client_row = db.get(Client, "desktop")
        client_row.name = "<script>alert('name')</script>"
        client_row.redirect_uris = "https://client.test/callback?x=<script>"
        db.add(DynamicClient(client_id="desktop"))
    login(client)
    page = client.get("/connections", base_url=BASE)
    assert b"Automatically registered" in page.data
    assert b"<script>" not in page.data and b"&lt;script&gt;" in page.data
    with application.extensions["db_session"].begin() as db:
        db.delete(db.get(DynamicClient, "desktop"))
        db.delete(db.get(Client, "desktop"))
    assert b"No connections yet" in client.get("/connections", base_url=BASE).data


def test_connections_use_cloudflare_admin_origin_guard(client, application):
    from test_cloudflare import HEADERS, ORIGIN, PEER
    login(client)
    application.config.update(CLOUDFLARE_PROXY_ENABLED=True, CLOUDFLARE_ORIGIN_SECRET=ORIGIN,
                              TRUSTED_PROXY_CIDRS=["10.20.0.0/24"])
    assert client.get("/connections", base_url=BASE, environ_overrides=PEER).status_code == 403
    assert client.post("/connections/desktop/disconnect", base_url=BASE, environ_overrides=PEER).status_code == 403
    assert client.get("/connections", base_url=BASE, headers=HEADERS, environ_overrides=PEER).status_code == 200
