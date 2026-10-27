"""Owner inspection and revocation of OAuth clients; no public account signup."""
from flask import abort, current_app, g, redirect, render_template, request, url_for
from sqlalchemy import func, select

from .auth import check_csrf, csrf, owner_required
from .models import Account, Client, DynamicClient, Profile, Subscription, Token, now
from .permissions import READ_SCOPE
from .registration import client_is_active, revoke_client


def install_connections(app):
    @app.get("/connections")
    @owner_required
    def connections():
        timestamp = now()
        scopes = (READ_SCOPE,)
        live_grants = g.db.execute(select(
            Token.client_id, func.count(func.distinct(Token.family))
        ).where(Token.owner_id == "owner", Token.revoked.is_(False),
                Token.refresh_expires_at > timestamp, Token.resource == current_app.config["RESOURCE"],
                Token.scope.in_(scopes)).group_by(Token.client_id)).all()
        grant_counts = dict(live_grants)

        configured_accounts = [item["id"] for item in current_app.config["ACCOUNTS"]]
        live_subscriptions = g.db.execute(select(
            Subscription.client_id, func.count(func.distinct(Subscription.id))
        ).join(Token, (Token.family == Subscription.token_family)
                & (Token.client_id == Subscription.client_id)
                & (Token.owner_id == Subscription.owner_id))
          .join(Account, Account.id == Subscription.account_id)
          .join(Profile, Profile.account_id == Subscription.account_id)
          .where(Subscription.owner_id == "owner", Subscription.active.is_(True),
                 Subscription.expires_at > timestamp, Subscription.account_id.in_(configured_accounts),
                 Account.owner_id == Subscription.owner_id, Token.resource == current_app.config["RESOURCE"],
                 Token.scope.in_(scopes), Token.revoked.is_(False), Token.refresh_expires_at > timestamp)
          .group_by(Subscription.client_id)).all() if configured_accounts else []
        subscription_counts = dict(live_subscriptions)
        rows = []
        for client in g.db.scalars(select(Client).order_by(Client.name, Client.client_id)):
            active = client_is_active(g.db, client.client_id, current_app.config["OAUTH_CLIENTS"],
                                      current_app.config["OAUTH_DYNAMIC_REGISTRATION_ENABLED"])
            grant_count = grant_counts.get(client.client_id, 0) if active else 0
            subscription_count = subscription_counts.get(client.client_id, 0) if active else 0
            rows.append(dict(client=client, active=bool(active),
                             status="Connected" if grant_count else "Not connected" if active else "Disconnected",
                             grant_count=grant_count, subscription_count=subscription_count,
                             dynamic=g.db.get(DynamicClient, client.client_id) is not None))
        return render_template("connections.html", connections=rows, csrf_token=csrf(),
                               notice="Client disconnected." if request.args.get("disconnected") == "1" else None)

    @app.post("/connections/<client_id>/disconnect")
    @owner_required
    def connection_disconnect(client_id):
        check_csrf()
        if not revoke_client(g.db, client_id):
            abort(404, "Client not found")
        return redirect(url_for("connections", disconnected="1"), code=303)
