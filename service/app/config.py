import json
import os
import re
from ipaddress import ip_network
from pathlib import PurePosixPath
from urllib.parse import urlsplit


def client_is_allowed(client_id, clients):
    return any(client.get("client_id") == client_id for client in clients)


def settings(overrides=None):
    env = os.environ
    c = {"DATABASE_URL": env.get("DATABASE_URL", "postgresql+psycopg://localhost/whatsapp"),
         "APP_INTERNAL_URL": env.get("APP_INTERNAL_URL", "http://whatsapp-mcp:8000"),
         "APP_BASE_URL": env.get("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
         "SECRET_KEY": env.get("SESSION_SECRET"), "TOKEN_ENCRYPTION_KEY": env.get("TOKEN_ENCRYPTION_KEY"),
         "INTERNAL_TOKEN": env.get("INTERNAL_TOKEN"), "ADMIN_EMAIL": env.get("ADMIN_EMAIL", "owner@example.invalid"),
         "ADMIN_PASSWORD_FILE": env.get("ADMIN_PASSWORD_FILE"), "ADMIN_PASSWORD": env.get("ADMIN_PASSWORD"),
         "ACCOUNTS": json.loads(env.get("ACCOUNTS_JSON", '[{"id":"personal","label":"Personal"}]')),
         "TRUSTED_PROXY_CIDRS": [x.strip() for x in env.get("TRUSTED_PROXY_CIDRS", "").split(",") if x.strip()],
         "LOGIN_PROXY_MODE": env.get("LOGIN_PROXY_MODE", ""),
         "LOGIN_PROXY_SECRET": env.get("LOGIN_PROXY_SECRET", ""),
         "CLOUDFLARE_PROXY_ENABLED": env.get("CLOUDFLARE_PROXY_ENABLED", "false"),
         "CLOUDFLARE_ORIGIN_SECRET": env.get("CLOUDFLARE_ORIGIN_SECRET", ""),
         "BRIDGE_URLS": json.loads(env.get("BRIDGE_URLS_JSON", "{}")),
         "OAUTH_CLIENTS": json.loads(env.get("OAUTH_CLIENTS_JSON", "[]")),
         "OAUTH_DYNAMIC_REGISTRATION_ENABLED": env.get("OAUTH_DYNAMIC_REGISTRATION_ENABLED", "true"),
         "WEBHOOK_RELAY_URL": env.get("WEBHOOK_RELAY_URL", ""),
         "WEBHOOK_RELAY_TOKEN": env.get("WEBHOOK_RELAY_TOKEN", ""),
         "MAX_CONTENT_LENGTH": 2 * 1024 * 1024, "SESSION_COOKIE_HTTPONLY": True,
         "SESSION_COOKIE_SAMESITE": "Lax", "PERMANENT_SESSION_LIFETIME": 3600,
         "ASR_ENABLED": env.get("ASR_ENABLED", "true"),
         "STORAGE_BACKEND": env.get("STORAGE_BACKEND", "s3"),
         "MEDIA_ROOT": env.get("MEDIA_ROOT", "/media"),
         "TESTING": False}
    for k in ("S3_ENDPOINT_URL", "S3_PUBLIC_ENDPOINT_URL", "S3_BUCKET", "S3_REGION", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ADDRESSING_STYLE"):
        c[k] = env.get(k)
    c.update(overrides or {})
    asr = c["ASR_ENABLED"]
    if isinstance(asr, str) and asr.lower() in ("true", "false", "1", "0"):
        asr = asr.lower() in ("true", "1")
    if type(asr) is not bool:
        raise ValueError("Invalid ASR_ENABLED")
    c["ASR_ENABLED"] = asr
    if c["STORAGE_BACKEND"] not in ("s3", "disk"):
        raise ValueError("Invalid STORAGE_BACKEND")
    media_root = c["MEDIA_ROOT"]
    if (not isinstance(media_root, str) or not media_root.startswith("/")
            or media_root.startswith("//") or media_root == "/"
            or str(PurePosixPath(media_root)) != media_root
            or ".." in PurePosixPath(media_root).parts
            or any(ord(char) <= 32 or ord(char) == 127 for char in media_root)):
        raise ValueError("MEDIA_ROOT must be a normalized absolute directory other than /")
    relay_url, relay_token = c["WEBHOOK_RELAY_URL"], c["WEBHOOK_RELAY_TOKEN"]
    if not isinstance(relay_url, str) or not isinstance(relay_token, str):
        raise ValueError("Invalid webhook relay configuration")
    if bool(relay_url) != bool(relay_token):
        raise ValueError("Webhook relay URL and dedicated token must be supplied together")
    if relay_url:
        try:
            relay = urlsplit(relay_url)
            valid_port = relay.port is None or 1 <= relay.port <= 65535
        except ValueError:
            raise ValueError("Invalid WEBHOOK_RELAY_URL") from None
        if (len(relay_url) > 2048 or relay.scheme not in ("http", "https") or not relay.hostname
                or relay.username or relay.password or relay.query or relay.fragment
                or relay.path not in ("", "/") or not valid_port
                or any(ord(char) <= 32 or ord(char) == 127 for char in relay_url)):
            raise ValueError("Invalid WEBHOOK_RELAY_URL")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", relay_token):
            raise ValueError("WEBHOOK_RELAY_TOKEN must contain 32 to 256 URL-safe characters")
        if relay_token in (c["SECRET_KEY"], c["INTERNAL_TOKEN"], c["TOKEN_ENCRYPTION_KEY"],
                           c["LOGIN_PROXY_SECRET"], c["CLOUDFLARE_ORIGIN_SECRET"]):
            raise ValueError("WEBHOOK_RELAY_TOKEN must be a dedicated secret")
        c["WEBHOOK_RELAY_URL"] = relay_url.rstrip("/")
    dynamic = c["OAUTH_DYNAMIC_REGISTRATION_ENABLED"]
    if isinstance(dynamic, str) and dynamic.lower() in ("true", "false", "1", "0"):
        dynamic = dynamic.lower() in ("true", "1")
    if type(dynamic) is not bool:
        raise ValueError("Invalid OAUTH_DYNAMIC_REGISTRATION_ENABLED")
    c["OAUTH_DYNAMIC_REGISTRATION_ENABLED"] = dynamic
    if any(client.get("client_id", "").startswith("dcr_") for client in c["OAUTH_CLIENTS"]):
        raise ValueError("Static client IDs cannot use reserved dcr_ prefix")
    enabled = c["CLOUDFLARE_PROXY_ENABLED"]
    if isinstance(enabled, str) and enabled.lower() in ("true", "false", "1", "0"):
        enabled = enabled.lower() in ("true", "1")
    if type(enabled) is not bool:
        raise ValueError("Invalid CLOUDFLARE_PROXY_ENABLED")
    c["CLOUDFLARE_PROXY_ENABLED"] = enabled
    if enabled:
        origin = c["CLOUDFLARE_ORIGIN_SECRET"]
        if not isinstance(origin, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", origin):
            raise ValueError("CLOUDFLARE_ORIGIN_SECRET must contain 32 to 256 URL-safe characters")
        if origin in (c["SECRET_KEY"], c["INTERNAL_TOKEN"], c["TOKEN_ENCRYPTION_KEY"], c["LOGIN_PROXY_SECRET"]):
            raise ValueError("CLOUDFLARE_ORIGIN_SECRET must be a dedicated secret")
        if not c["TRUSTED_PROXY_CIDRS"] or any(ip_network(value, strict=True).prefixlen == 0 for value in c["TRUSTED_PROXY_CIDRS"]):
            raise ValueError("Cloudflare requires narrow TRUSTED_PROXY_CIDRS")
    if c["LOGIN_PROXY_MODE"] not in ("", "direct", "signed", "envoy"):
        raise ValueError("Invalid LOGIN_PROXY_MODE")
    if c["LOGIN_PROXY_MODE"] in ("signed", "envoy") and not c["TRUSTED_PROXY_CIDRS"]:
        raise ValueError("LOGIN_PROXY_MODE requires TRUSTED_PROXY_CIDRS")
    if c["LOGIN_PROXY_MODE"] == "signed" and (not c["LOGIN_PROXY_SECRET"] or len(c["LOGIN_PROXY_SECRET"]) < 32):
        raise ValueError("LOGIN_PROXY_SECRET must contain at least 32 characters")
    for k in ("SECRET_KEY", "TOKEN_ENCRYPTION_KEY", "INTERNAL_TOKEN"):
        if not c[k] or len(c[k]) < 32:
            raise ValueError(f"{k} must be supplied with at least 32 characters")
    base = urlsplit(c["APP_BASE_URL"])
    if base.scheme != "https" and base.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("APP_BASE_URL must use HTTPS outside loopback")
    c["SESSION_COOKIE_SECURE"] = base.scheme == "https"
    internal = urlsplit(c["APP_INTERNAL_URL"])
    if not internal.hostname or internal.scheme not in ("http", "https") or internal.username or internal.password:
        raise ValueError("Invalid APP_INTERNAL_URL")
    c["TRUSTED_HOSTS"] = [base.hostname, internal.hostname]
    c["RESOURCE"] = c["APP_BASE_URL"] + "/mcp"
    return c
