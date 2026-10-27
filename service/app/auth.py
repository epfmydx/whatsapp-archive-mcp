"""Authlib performs OAuth grant, PKCE and client validation; tokens are hashed at rest."""
import hashlib
import hmac
import secrets
import ipaddress
import re
from functools import wraps
from urllib.parse import urlsplit
from flask import current_app, g, request, session, abort, redirect, render_template, jsonify
from sqlalchemy import select, update
from authlib.integrations.flask_oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oauth2.rfc6749.errors import InvalidRequestError
from werkzeug.security import check_password_hash
from .registration import client_is_active, validate_redirect_uri
from .proxy import login_source
from .models import Owner, Client, Code, Token, RateLimit, now, uid
from .permissions import READ_SCOPE, SUPPORTED_SCOPES, has_scope, normalize_scope


def digest(value): return hashlib.sha256(value.encode()).hexdigest()


def active_client(client_id):
    return client_is_active(g.db, client_id, current_app.config["OAUTH_CLIENTS"],
                            current_app.config["OAUTH_DYNAMIC_REGISTRATION_ENABLED"], lock=True)


def csrf():
    if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    expected = session.get("csrf")
    if not expected or not hmac.compare_digest(expected.encode(), request.form.get("csrf", "").encode()):
        abort(400, "Invalid form token")


def owner_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if session.get("owner") != "owner": return redirect("/login")
        return fn(*args, **kwargs)
    wrapped._owner_only = True
    return wrapped


def internal_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        expected = "Bearer " + current_app.config["INTERNAL_TOKEN"]
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected.encode()): abort(401)
        return fn(*args, **kwargs)
    return wrapped


def bearer():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "): abort(401)
    token = g.db.scalar(select(Token).where(Token.access_hash == digest(header[7:])))
    if (not token or token.revoked or token.is_expired() or token.owner_id != "owner"
            or token.resource != current_app.config["RESOURCE"] or not has_scope(token.scope, READ_SCOPE)
            or not active_client(token.client_id)):
        abort(401)
    return token


def consent_callback_origin(grant):
    """Build CSP only from the exact callback already checked by Authlib."""
    client = grant.request.client
    uri = grant.request.payload.redirect_uri or client.get_default_redirect_uri()
    validate_redirect_uri(uri)
    if not client.check_redirect_uri(uri) or any(ord(char) <= 32 for char in uri):
        raise ValueError("Invalid callback origin")
    parsed = urlsplit(uri)
    host = parsed.hostname
    if not host or parsed.username or parsed.password or parsed.fragment or "*" in host:
        raise ValueError("Invalid callback origin")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Invalid callback origin")
    host = host.encode("idna").decode("ascii")
    if ":" in host:
        ipaddress.IPv6Address(host)
        host = "["+host+"]"
    elif not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("Invalid callback origin")
    return parsed.scheme+"://"+host+(":"+str(parsed.port) if parsed.port else "")


class S256Challenge(CodeChallenge):
    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]
    def validate_code_challenge(self, grant, redirect_uri):
        super().validate_code_challenge(grant, redirect_uri)
        if grant.request.payload.data.get("code_challenge_method") != "S256":
            raise InvalidRequestError("PKCE S256 required")
    def get_authorization_code_challenge(self, code): return code.challenge
    def get_authorization_code_challenge_method(self, code): return code.method


class CodeGrant(AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]
    def save_authorization_code(self, code, request):
        data = request.payload.data
        g.db.add(Code(code=digest(code), client_id=request.client.client_id, owner_id=request.user.id,
                      redirect_uri=request.payload.redirect_uri or request.client.get_default_redirect_uri(),
                      scope=g.approved_oauth_scope,
                      challenge=data["code_challenge"], method=data["code_challenge_method"]))
    def query_authorization_code(self, code, client):
        if not active_client(client.client_id): return None
        item = g.db.scalar(select(Code).where(Code.code == digest(code), Code.client_id == client.client_id).with_for_update())
        return item if item and not item.is_expired() else None
    def delete_authorization_code(self, code): g.db.delete(code)
    def authenticate_user(self, code): return g.db.get(Owner, code.owner_id)


class RefreshGrant(RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]
    INCLUDE_NEW_REFRESH_TOKEN = True
    def authenticate_refresh_token(self, value):
        item = g.db.scalar(select(Token).where(Token.refresh_hash == digest(value)))
        if not item or not active_client(item.client_id): return None
        # Match disconnect/bootstrap's Client -> Token lock order. Refresh the
        # token after acquiring both locks to observe a concurrent revocation.
        item = g.db.scalar(select(Token).where(Token.refresh_hash == digest(value))
                           .with_for_update().execution_options(populate_existing=True))
        if item and item.revoked:
            g.db.execute(update(Token).where(Token.family == item.family).values(revoked=True))
            return None
        if item and item.refresh_expires_at > now():
            g.refresh_family = item.family
            return item
        return None
    def authenticate_user(self, token): return g.db.get(Owner, token.owner_id)
    def revoke_old_credential(self, token): token.revoked = True


def install_auth(app):
    app.config.update(OAUTH2_REFRESH_TOKEN_GENERATOR=True, OAUTH2_TOKEN_EXPIRES_IN={"authorization_code": 900, "refresh_token": 900})
    def query_client(cid): return active_client(cid)
    def save_token(data, req):
        scope = normalize_scope(data.get("scope"), default_read=False)
        if not scope:
            raise InvalidRequestError("Invalid granted scope")
        g.db.add(Token(client_id=req.client.client_id, owner_id=req.user.id,
                       access_hash=digest(data["access_token"]), refresh_hash=digest(data["refresh_token"]),
                       family=getattr(g, "refresh_family", None) or uid(), resource=app.config["RESOURCE"],
                       scope=scope, expires_at=now()+900, refresh_expires_at=now()+30*86400))
    server = AuthorizationServer(app, query_client=query_client, save_token=save_token)
    server.register_grant(CodeGrant, [S256Challenge(required=True)])
    server.register_grant(RefreshGrant)

    @app.route("/.well-known/oauth-authorization-server")
    def metadata():
        base = app.config["APP_BASE_URL"]
        extra = {"registration_endpoint": base+"/oauth/register"} if app.config["OAUTH_DYNAMIC_REGISTRATION_ENABLED"] and urlsplit(base).scheme == "https" else {}
        return jsonify(**extra, issuer=base, authorization_endpoint=base+"/oauth/authorize", token_endpoint=base+"/oauth/token",
                       revocation_endpoint=base+"/oauth/revoke", response_types_supported=["code"],
                       grant_types_supported=["authorization_code", "refresh_token"],
                       code_challenge_methods_supported=["S256"], token_endpoint_auth_methods_supported=["none"],
                       scopes_supported=list(SUPPORTED_SCOPES))

    @app.route("/.well-known/oauth-protected-resource")
    @app.route("/.well-known/oauth-protected-resource/mcp")
    def resource_metadata():
        return jsonify(resource=app.config["RESOURCE"], authorization_servers=[app.config["APP_BASE_URL"]], scopes_supported=list(SUPPORTED_SCOPES))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            check_csrf()
            key = digest("login:" + login_source(request.environ, app.config))
            row = g.db.get(RateLimit, key, with_for_update=True)
            window = now()//900
            if row is None:
                row = RateLimit(key=key, window=window, count=0); g.db.add(row); g.db.flush()
            if row.window != window: row.window, row.count = window, 0
            row.count += 1
            if row.count > 10: abort(429)
            owner = g.db.get(Owner, "owner")
            valid = owner and hmac.compare_digest(request.form.get("email", "").lower().encode(), owner.email.lower().encode())
            # Check the expensive hash even for a wrong email.
            password_ok = owner and check_password_hash(owner.password_hash, request.form.get("password", ""))
            if valid and password_ok:
                target = session.get("authorize_next", "/")
                session.clear(); session["owner"] = "owner"; session.permanent = True
                return redirect(target)
            error = "Email or password is incorrect."
        return render_template("login.html", csrf_token=csrf(), error=error)

    @app.post("/logout")
    @owner_required
    def logout():
        check_csrf(); session.clear(); return redirect("/login")

    @app.route("/oauth/authorize", methods=["GET", "POST"])
    def authorize():
        if session.get("owner") != "owner":
            session["authorize_next"] = request.full_path
            return redirect("/login")
        if request.values.get("resource") != app.config["RESOURCE"]: abort(400, "Invalid resource")
        requested_scope = normalize_scope(request.values.get("scope"))
        if not requested_scope: abort(400, "Invalid scope")
        if request.method == "POST":
            check_csrf()
            grant = server.get_consent_grant(end_user=g.db.get(Owner, "owner"))
            g.oauth_form_origin = consent_callback_origin(grant)
            # No write scope is offered, even if a caller tampers with consent.
            g.approved_oauth_scope = READ_SCOPE
            return server.create_authorization_response(grant=grant, grant_user=g.db.get(Owner, "owner") if request.form.get("approve") == "yes" else None)
        grant = server.get_consent_grant(end_user=g.db.get(Owner, "owner"))
        g.oauth_form_origin = consent_callback_origin(grant)
        return render_template("oauth-consent.html", client_name=grant.request.client.name,
                               callback=grant.request.payload.redirect_uri or grant.request.client.get_default_redirect_uri(),
                               csrf_token=csrf())

    @app.post("/oauth/token")
    def token_endpoint():
        if request.form.get("resource") != app.config["RESOURCE"]: return jsonify(error="invalid_target"), 400
        if request.form.get("scope") and not normalize_scope(request.form["scope"], default_read=False):
            return jsonify(error="invalid_scope"), 400
        return server.create_token_response()

    @app.post("/oauth/revoke")
    def revoke():
        value = request.form.get("token", "")
        item = g.db.scalar(select(Token).where((Token.access_hash == digest(value)) | (Token.refresh_hash == digest(value))).with_for_update())
        if item and item.client_id == request.form.get("client_id"):
            g.db.execute(update(Token).where(Token.family == item.family).values(revoked=True))
        return "", 200
