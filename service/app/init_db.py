"""Explicit, additive v1 schema bootstrap. Never drops or truncates tables."""
import re
from pathlib import Path
from werkzeug.security import generate_password_hash
from sqlalchemy import select
from .config import settings, client_is_allowed
from .models import Base, Owner, Client, DynamicClient, Account, Profile, database, uid
from .registration import revoke_client, validate_redirect_uri


def initialize(c):
    engine, sessions = database(c["DATABASE_URL"])
    Base.metadata.create_all(engine)
    with sessions.begin() as db:
        # One existing owner row serializes bootstrap and public registrations.
        if db.get(Owner, "owner", with_for_update=True) is None:
            password = Path(c["ADMIN_PASSWORD_FILE"]).read_text().rstrip("\r\n") if c.get("ADMIN_PASSWORD_FILE") else c.get("ADMIN_PASSWORD")
            if not password or len(password) < 16: raise ValueError("Bootstrap owner password must contain at least 16 characters")
            db.add(Owner(id="owner", email=c["ADMIN_EMAIL"].lower(), password_hash=generate_password_hash(password)))
            db.flush()
        for registered in db.scalars(select(Client).with_for_update()):
            dynamic = db.get(DynamicClient, registered.client_id)
            permitted = c["OAUTH_DYNAMIC_REGISTRATION_ENABLED"] if dynamic else client_is_allowed(registered.client_id, c["OAUTH_CLIENTS"])
            if not permitted:
                revoke_client(db, registered.client_id)
        for a in c["ACCOUNTS"]:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", a["id"]): raise ValueError("Unsafe account ID")
            existing = db.get(Account, a["id"])
            # Config labels seed new runtimes only. Renaming a profile must survive
            # every later bootstrap and must never change its stable account ID.
            if existing is None:
                db.add(Account(id=a["id"], label=a["label"], owner_id="owner"))
                db.flush()
            if not a.get("profileSlot", False) and db.get(Profile, a["id"]) is None:
                db.add(Profile(account_id=a["id"], creation_key=uid()))
        for client in c["OAUTH_CLIENTS"]:
            if client["client_id"].startswith("dcr_"):
                raise ValueError("Static client IDs cannot use reserved dcr_ prefix")
            uris = client["redirect_uris"]
            if not uris: raise ValueError("Client requires exact redirect URIs")
            for uri in uris:
                validate_redirect_uri(uri)
            existing = db.get(Client, client["client_id"])
            if existing: existing.name, existing.redirect_uris = client["name"], "\n".join(uris)
            else: db.add(Client(client_id=client["client_id"], name=client["name"], redirect_uris="\n".join(uris)))
    engine.dispose()

if __name__ == "__main__": initialize(settings())
