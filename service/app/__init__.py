from datetime import datetime, timedelta, timezone
from authlib.oauth2.rfc6749.errors import OAuth2Error
from cryptography.fernet import Fernet
from flask import g, jsonify
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException
from .config import settings
from .proxy import TrustedSchemeProxy
from .models import database, Owner
from .auth import install_auth
from .registration import install_registration
from .connections import install_connections
from .api import install_api
from .storage import install_storage
from .profiles import install_profiles
from .cloudflare import install_admin_origin_guard
from .cache import StaticAssetFlask, StaticAssetSessionInterface, apply_cache_policy
from .identities import install_identity_routes
from .observability import new_correlation_id, log_callback, install_safe_framework_logging


def create_app(overrides=None):
    app = StaticAssetFlask(__name__)
    app.session_interface = StaticAssetSessionInterface()
    app.config.update(settings(overrides))
    app.wsgi_app = TrustedSchemeProxy(app.wsgi_app, app.config["TRUSTED_PROXY_CIDRS"])
    Fernet(app.config["TOKEN_ENCRYPTION_KEY"].encode())
    engine, sessions = database(app.config["DATABASE_URL"])
    app.extensions.update(db_engine=engine, db_session=sessions)
    install_safe_framework_logging(app)

    def sanitized_exception_log(exc_info):
        # Flask's default exception logger includes exception text/tracebacks,
        # which can contain provider responses or database parameters.
        correlation = getattr(g, "correlation_id", None) or new_correlation_id()
        if hasattr(g, "mcp_started"):
            g.mcp_reason = "internal_error"
        log_callback("unknown", "error", correlation, 0, reason="internal_error", component="api")
    app.log_exception = sanitized_exception_log

    @app.template_filter("malaysia_time")
    def malaysia_time(value):
        if value is None:
            return "Not yet recorded"
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return "Unknown"
            return stamp.astimezone(timezone(timedelta(hours=8))).strftime("%d %b %Y, %H:%M:%S MYT")
        except (ValueError, TypeError, AttributeError):
            return "Unknown"

    @app.before_request
    def start_session(): g.db = sessions()

    @app.after_request
    def finish(response):
        if hasattr(g, "db"):
            if response.status_code < 500: g.db.commit()
            else: g.db.rollback()
        apply_cache_policy(response)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                                  "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"})
        if getattr(g, "oauth_form_origin", None):
            response.headers["Content-Security-Policy"] = response.headers["Content-Security-Policy"].replace(
                "form-action 'self'", "form-action 'self' "+g.oauth_form_origin)
        if app.config["SESSION_COOKIE_SECURE"]: response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.teardown_request
    def close_session(error):
        if hasattr(g, "db"): g.db.close()

    @app.errorhandler(ValueError)
    def value_error(error): return jsonify(error="invalid_request", message=str(error)[:200]), 400

    @app.errorhandler(OAuth2Error)
    def oauth_error(error): return jsonify(dict(error.get_body())), error.status_code, dict(error.get_headers())

    @app.errorhandler(IntegrityError)
    def conflict(error):
        g.db.rollback()
        return jsonify(error="conflict_retry"), 409

    @app.errorhandler(HTTPException)
    def http_error(error):
        response = jsonify(error=error.name, message=error.description)
        response.status_code = error.code
        if error.code == 401: response.headers["WWW-Authenticate"] = f'Bearer resource_metadata="{app.config["APP_BASE_URL"]}/.well-known/oauth-protected-resource", scope="whatsapp:read"'
        return response

    @app.get("/healthz")
    def health(): return jsonify(status="ok")

    @app.get("/readyz")
    def ready():
        try:
            if g.db.get(Owner, "owner") is None: return jsonify(status="not_ready"), 503
        except Exception:
            g.db.rollback()
            return jsonify(status="not_ready"), 503
        return jsonify(status="ready")

    install_registration(app)
    install_auth(app)
    install_connections(app)
    install_profiles(app)
    install_api(app)
    install_storage(app)
    install_identity_routes(app)
    install_admin_origin_guard(app)
    return app
