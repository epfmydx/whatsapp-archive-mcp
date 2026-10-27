"""Owner page statistics remain account-scoped and reflect live authorization."""
import json

from app.models import Account, AccountHealth, Message, Owner, Subscription, Token, now
from test_auth import BASE, login


def _token(client_id, family, *, owner="owner", resource=BASE + "/mcp", scope="whatsapp:read",
           refresh_expiry=None, revoked=False, marker="x"):
    return Token(client_id=client_id, owner_id=owner, access_hash=marker * 64,
                 refresh_hash=(marker.upper() + "y") * 32, family=family, resource=resource,
                 scope=scope, expires_at=now() + 900,
                 refresh_expires_at=refresh_expiry if refresh_expiry is not None else now() + 3600,
                 revoked=revoked)


def _subscription(id, family, *, client="desktop", account="personal", owner="owner",
                  expiry=None, active=True, secret="never-display-this"):
    return Subscription(id=id, owner_id=owner, client_id=client, token_family=family,
                        idempotency_key=id, callback="https://callback.test/events",
                        destination_ip="8.8.8.8", secret=secret, account_id=account,
                        event_type="whatsapp.message.received",
                        expires_at=expiry if expiry is not None else now() + 3600,
                        active=active)


def test_profiles_show_health_and_account_scoped_archive_counts(client, application):
    with application.extensions["db_session"].begin() as db:
        db.add_all([
            Message(account_id="personal", message_id="text", chat_jid="chat", sender_jid="sender",
                    timestamp="2026-10-27T00:00:00Z", text="synthetic"),
            Message(account_id="personal", message_id="media", chat_jid="chat", sender_jid="sender",
                    timestamp="2026-10-27T00:01:00Z", media=json.dumps({"object_key": "private-key", "size": 123})),
            Message(account_id="personal", message_id="empty-media", chat_jid="chat", sender_jid="sender",
                    timestamp="2026-10-27T00:02:00Z", media="{}"),
            AccountHealth(account_id="personal", last_bridge_seen_at=now(), last_message_ingested_at=now(),
                          whatsapp_connected=True, whatsapp_authenticated=True, pending_count=0),
        ])
    login(client)
    response = client.get("/", base_url=BASE)
    assert response.status_code == 200
    assert b"Bridge health" in response.data and b"Healthy" in response.data
    assert b"Pending archive work</dt><dd>0" in response.data
    assert b"Archived messages</dt><dd>3" in response.data
    assert b"Media records</dt><dd>1" in response.data
    assert b"private-key" not in response.data and b"123" not in response.data


def test_stale_health_is_not_presented_as_healthy_and_login_is_required(client, application):
    assert client.get("/", base_url=BASE).location == "/login"
    with application.extensions["db_session"].begin() as db:
        db.add(AccountHealth(account_id="personal", last_bridge_seen_at=now() - 100,
                             whatsapp_connected=True, whatsapp_authenticated=True, pending_count=0))
    login(client)
    response = client.get("/", base_url=BASE)
    assert response.status_code == 200
    assert b"Stale" in response.data and b"Healthy" not in response.data


def test_zero_archive_totals_are_known_while_missing_health_is_unknown(client):
    login(client)
    response = client.get("/", base_url=BASE)
    assert response.status_code == 200
    assert b"Bridge health</dt><dd>Unknown" in response.data
    assert b"Pending archive work</dt><dd>Unknown" in response.data
    assert b"Archived messages</dt><dd>0" in response.data
    assert b"Media records</dt><dd>0" in response.data


def test_connections_count_unique_live_grant_families_and_valid_subscriptions(client, application):
    with application.extensions["db_session"].begin() as db:
        db.add_all([
            _token("desktop", "family-live", marker="a"),
            _token("desktop", "family-live", marker="b"),  # A rotated-family row is not a second session.
            _token("desktop", "family-expired", refresh_expiry=now() - 1, marker="c"),
            _token("desktop", "family-revoked", revoked=True, marker="d"),
            _token("desktop", "family-wrong-resource", resource="https://elsewhere.test/mcp", marker="e"),
            _token("desktop", "family-wrong-owner", owner="other-owner", marker="f"),
            _token("disabled-client", "disabled-family", marker="g"),
            _subscription("valid-sub", "family-live"),
            _subscription("expired-sub", "family-live", expiry=now() - 1),
            _subscription("inactive-sub", "family-live", active=False),
            _subscription("wrong-family-sub", "family-expired"),
            _subscription("unassigned-sub", "family-live", account="unassigned"),
            _subscription("wrong-owner-sub", "family-wrong-owner", owner="owner"),
            _subscription("disabled-sub", "disabled-family", client="disabled-client"),
        ])
        from app.models import Client
        db.add(Client(client_id="disabled-client", name="Disabled", redirect_uris=""))
        db.add(Owner(id="other-owner", email="other@example.test", password_hash="not-a-login"))
    login(client)
    response = client.get("/connections", base_url=BASE)
    assert response.status_code == 200
    body = response.data
    assert b"Active access grants</dt><dd>1" in body
    assert b"Active event subscriptions</dt><dd>1" in body
    assert b"Active access grants</dt><dd>0" in body
    assert b"never-display-this" not in body and b"8.8.8.8" not in body
    assert b"family-live" not in body and b"access_hash" not in body
