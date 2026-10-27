"""RFC 7591 registration for public, owner-approved, PKCE-only clients.

This public-client profile deliberately defaults an omitted endpoint auth method
to ``none`` instead of RFC 7591's confidential-client default. The server never
issues a client secret. Registration grants no archive access or owner account.
"""
import ipaddress
import json
import re
import secrets
import unicodedata
from urllib.parse import urlsplit, unquote

from flask import g, request, jsonify
from sqlalchemy import select, update, delete, func
from .config import client_is_allowed
from .models import Owner, Client, DynamicClient, Token, Code, Subscription, RateLimit, now
from .permissions import normalize_scope

MAX_BODY = 8192
MAX_REDIRECTS = 8
REGISTRATION_WINDOW = 900
REGISTRATION_ATTEMPTS = 10
MAX_LIVE_CLIENTS = 50
MAX_PENDING_CLIENTS = 20


def client_is_active(db, client_id, clients, dynamic_enabled=True, lock=False):
    """One persisted-client policy for grants, bearer access, and dispatch.

    ``None`` retains the internal dispatch API's optional static-allowlist behavior;
    HTTP and controller callers always supply their current configured allowlist.
    """
    client = db.get(Client, client_id, with_for_update=lock)
    if not client or not client.enabled:
        return None
    dynamic = db.get(DynamicClient, client_id)
    if dynamic:
        return client if dynamic_enabled else None
    return client if clients is None or client_is_allowed(client_id, clients) else None


def revoke_client(db, client_id):
    """Disconnect without deleting historical foreign keys or registration IDs."""
    client = db.get(Client, client_id, with_for_update=True)
    if not client:
        return False
    client.redirect_uris = ""
    families = select(Token.family).where(Token.client_id == client_id)
    db.execute(update(Token).where(Token.family.in_(families)).values(revoked=True))
    db.execute(delete(Code).where(Code.client_id == client_id))
    db.execute(update(Subscription).where(Subscription.client_id == client_id).values(active=False))
    return True


def validate_redirect_uri(uri):
    """Validate without normalization: OAuth later compares the exact string."""
    if not isinstance(uri, str) or not 1 <= len(uri) <= 2048:
        raise ValueError("Invalid redirect URI")
    decoded = unquote(uri)
    if any(c in uri for c in ("\\", "*", "#")) or "\\" in decoded or any(unicodedata.category(c).startswith("C") for c in decoded):
        raise ValueError("Invalid redirect URI")
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in uri):
        raise ValueError("Invalid redirect URI")
    if re.search(r"%(?![0-9a-fA-F]{2})", uri):
        raise ValueError("Invalid redirect URI")
    try:
        parsed = urlsplit(uri)
        host, port = parsed.hostname, parsed.port
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.netloc.endswith(":")):
            raise ValueError("Invalid redirect URI")
        if ":" in host:
            ipaddress.IPv6Address(host)
            if not re.fullmatch(r"\[[0-9a-fA-F:]+\](?::[0-9]+)?", parsed.netloc):
                raise ValueError("Invalid redirect URI")
        else:
            ascii_host = host.encode("idna").decode("ascii")
            if (len(ascii_host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                               for label in ascii_host.rstrip(".").split("."))):
                raise ValueError("Invalid redirect URI")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid redirect URI")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("Redirect URI must use HTTPS or loopback HTTP")
    except (ValueError, UnicodeError):
        raise ValueError("Invalid redirect URI") from None
    return uri


def _metadata(data):
    if not isinstance(data, dict):
        raise ValueError("Registration metadata must be an object")
    redirects = data.get("redirect_uris")
    if not isinstance(redirects, list) or not 1 <= len(redirects) <= MAX_REDIRECTS:
        raise ValueError("Provide 1 to 8 exact redirect URIs")
    redirects = [validate_redirect_uri(uri) for uri in redirects]
    if len(set(redirects)) != len(redirects):
        raise ValueError("Duplicate redirect URIs")
    name = data.get("client_name", "OAuth client")
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(unicodedata.category(c).startswith("C") for c in name):
        raise ValueError("Invalid client name")
    if data.get("token_endpoint_auth_method", "none") != "none":
        raise ValueError("Only public client authentication method none is supported")
    scope = normalize_scope(data.get("scope"))
    if not scope:
        raise ValueError("Unsupported scope")
    grants = data.get("grant_types", ["authorization_code", "refresh_token"])
    if (not isinstance(grants, list) or not grants or any(not isinstance(v, str) for v in grants)
            or len(set(grants)) != len(grants) or "authorization_code" not in grants
            or set(grants) - {"authorization_code", "refresh_token"}):
        raise ValueError("Only authorization_code and refresh_token grants are supported")
    if data.get("response_types", ["code"]) != ["code"]:
        raise ValueError("Only code response type is supported")
    for field in ("client_uri", "logo_uri", "tos_uri", "policy_uri", "software_id", "software_version"):
        if field in data and (not isinstance(data[field], str) or len(data[field]) > 2048):
            raise ValueError("Invalid optional client metadata")
    if "contacts" in data and (not isinstance(data["contacts"], list) or len(data["contacts"]) > 8
                                or any(not isinstance(v, str) or len(v) > 254 for v in data["contacts"])):
        raise ValueError("Invalid client contacts")
    if any(field in data for field in ("client_secret", "jwks", "jwks_uri", "software_statement")):
        raise ValueError("Unsupported client authentication metadata")
    # Unsupported extension metadata is ignored as required by RFC 7591. Nothing
    # besides this bounded public display name and validated callbacks is stored.
    # Provision this fixed server profile, including refresh rotation, even when
    # a client requests only authorization_code. Return the actual grant set.
    return {"client_name": name, "redirect_uris": redirects, "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": scope, "token_endpoint_auth_method": "none"}


def install_registration(app):
    @app.post("/oauth/register")
    def register():
        if not app.config["OAUTH_DYNAMIC_REGISTRATION_ENABLED"]:
            return jsonify(error="registration_not_supported"), 404
        if urlsplit(app.config["APP_BASE_URL"]).scheme != "https" or not request.is_secure:
            return jsonify(error="invalid_client_metadata", error_description="HTTPS is required"), 400
        # Bound the body before taking the cross-replica database lock. A slow
        # sender must not hold the owner/bootstrap registration admission lock.
        body_error = None
        raw = b""
        if request.mimetype != "application/json":
            body_error = ("Use application/json", 415)
        elif request.content_length is not None and request.content_length > MAX_BODY:
            body_error = ("Registration body is too large", 413)
        else:
            raw = request.stream.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                body_error = ("Registration body is too large", 413)
        # A durable existing row serializes replicas even when the rate-limit row
        # has not yet been created. Unbootstrapped instances fail closed.
        if not g.db.get(Owner, "owner", with_for_update=True):
            return jsonify(error="temporarily_unavailable"), 503
        window = now() // REGISTRATION_WINDOW
        limit = g.db.get(RateLimit, "oauth:dynamic-registration")
        if limit is None:
            limit = RateLimit(key="oauth:dynamic-registration", window=window, count=0)
            g.db.add(limit)
        if limit.window != window:
            limit.window, limit.count = window, 0
        if limit.count >= REGISTRATION_ATTEMPTS:
            return jsonify(error="temporarily_unavailable"), 429, {"Retry-After": str(REGISTRATION_WINDOW - now() % REGISTRATION_WINDOW)}
        limit.count += 1
        if body_error:
            return jsonify(error="invalid_client_metadata", error_description=body_error[0]), body_error[1]
        try:
            # Ambiguous duplicate JSON members are rejected before persistence.
            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj: raise ValueError("Duplicate metadata member")
                    obj[key] = value
                return obj
            def invalid_constant(value):
                raise ValueError("Invalid JSON constant")
            metadata = _metadata(json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant))
        except (ValueError, UnicodeError, RecursionError):
            return jsonify(error="invalid_client_metadata", error_description="Invalid public client metadata"), 400
        live = select(DynamicClient.client_id).join(Client).where(Client.redirect_uris != "")
        established = select(Token.client_id).where(Token.revoked.is_(False), Token.refresh_expires_at > now())
        live_count = g.db.scalar(select(func.count()).select_from(live.subquery()))
        pending_count = g.db.scalar(select(func.count()).select_from(live.where(DynamicClient.client_id.not_in(established)).subquery()))
        if live_count >= MAX_LIVE_CLIENTS or pending_count >= MAX_PENDING_CLIENTS:
            return jsonify(error="temporarily_unavailable", error_description="Owner must disconnect unused clients"), 429, {"Retry-After": "900"}
        # Serialize with bootstrap, reserve generated namespace, and explicitly
        # check for a collision without ever overwriting a persisted registration.
        client_id = "dcr_" + secrets.token_urlsafe(32)
        while g.db.get(Client, client_id) is not None:
            client_id = "dcr_" + secrets.token_urlsafe(32)
        issued_at = now()
        g.db.add(Client(client_id=client_id, name=metadata["client_name"], redirect_uris="\n".join(metadata["redirect_uris"])))
        g.db.flush()
        g.db.add(DynamicClient(client_id=client_id, created_at=issued_at))
        return jsonify(metadata | {"client_id": client_id, "client_id_issued_at": issued_at}), 201
