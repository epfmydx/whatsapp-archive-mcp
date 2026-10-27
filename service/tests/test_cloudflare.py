import pytest
from sqlalchemy import select

from app.config import settings
from app.models import RateLimit
from app.proxy import login_source
from test_auth import BASE


ORIGIN = "dedicated-cloudflare-origin-secret-1234567890"
HEADERS = {"X-WhatsApp-Origin": ORIGIN, "X-Forwarded-Proto": "https", "CF-Connecting-IP": "8.8.8.8"}
PEER = {"REMOTE_ADDR": "10.20.0.5"}


@pytest.fixture
def guarded(application):
    application.config.update(CLOUDFLARE_PROXY_ENABLED=True, CLOUDFLARE_ORIGIN_SECRET=ORIGIN,
                              TRUSTED_PROXY_CIDRS=["10.20.0.0/24"])
    return application.test_client()


@pytest.mark.parametrize("path", ["/", "/login", "/profiles/new", "/profiles/personal", "/accounts/personal/qr.png", "/logout", "/oauth/authorize"])
def test_admin_routes_require_origin_before_session(guarded, path):
    response = guarded.get(path, base_url=BASE, environ_overrides=PEER)
    # /logout is POST-only: guard valid endpoint by its supported method.
    if path == "/logout":
        response = guarded.post(path, base_url=BASE, environ_overrides=PEER)
    assert response.status_code == 403
    assert response.json["message"] == "cloudflare_proxy_required"
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_origin_does_not_replace_password_session(guarded):
    assert guarded.get("/login", base_url=BASE, headers=HEADERS, environ_overrides=PEER).status_code == 200
    response = guarded.get("/", base_url=BASE, headers=HEADERS, environ_overrides=PEER)
    assert response.status_code == 302 and response.location == "/login"


@pytest.mark.parametrize("header,value,status,message", [
    ("X-WhatsApp-Origin", "wrong", 403, "cloudflare_proxy_required"),
    ("X-WhatsApp-Origin", ORIGIN+","+ORIGIN, 403, "cloudflare_proxy_required"),
    ("X-Forwarded-Proto", "http", 403, "cloudflare_https_required"),
    ("X-Forwarded-Proto", "https,https", 403, "cloudflare_https_required"),
    *[("CF-Connecting-IP", value, 400, "invalid_cloudflare_client_ip") for value in (
        "", "127.0.0.1", "10.0.0.1", "169.254.1.1", "224.0.0.1", "::ffff:127.0.0.1",
        "fe80::1%eth0", "8.8.8.8,1.1.1.1", "8.8.8.8:443", " 8.8.8.8", "2001:db8::1")],
])
def test_invalid_forwarded_metadata(guarded, header, value, status, message):
    response = guarded.get("/login", base_url=BASE, headers={**HEADERS, header: value}, environ_overrides=PEER)
    assert response.status_code == status and response.json["message"] == message


def test_untrusted_peer_cannot_supply_origin_headers(guarded):
    response = guarded.get("/login", base_url=BASE, headers=HEADERS, environ_overrides={"REMOTE_ADDR": "8.8.4.4"})
    assert response.status_code == 403


@pytest.mark.parametrize("path,method,status", [("/healthz", "get", 200), ("/readyz", "get", 200),
    ("/.well-known/oauth-authorization-server", "get", 200), ("/mcp", "post", 401),
    ("/oauth/token", "post", 400), ("/oauth/revoke", "post", 200),
    ("/internal/accounts/status", "post", 401)])
def test_machine_routes_keep_own_auth(guarded, path, method, status):
    response = getattr(guarded, method)(path, base_url=BASE)
    assert response.status_code == status, response.data
    assert b"cloudflare_proxy_required" not in response.data


def test_cf_identity_controls_login_rate_limit(application, guarded):
    for address in ("8.8.8.8", "1.1.1.1", "::ffff:8.8.8.8"):
        headers = {**HEADERS, "CF-Connecting-IP": address, "X-Envoy-External-Address": "9.9.9.9", "X-Forwarded-For": "4.4.4.4"}
        guarded.get("/login", base_url=BASE, headers=headers, environ_overrides=PEER)
        with guarded.session_transaction(base_url=BASE) as session:
            csrf = session["csrf"]
        response = guarded.post("/login", base_url=BASE, headers=headers, environ_overrides=PEER,
                                data={"csrf": csrf, "email": "wrong", "password": "wrong"})
        assert response.status_code == 200
    with application.extensions["db_session"]() as db:
        assert sorted(row.count for row in db.scalars(select(RateLimit))) == [1, 2]
    with pytest.raises(ValueError):
        login_source({"REMOTE_ADDR": "8.8.8.8", "HTTP_CF_CONNECTING_IP": "1.1.1.1"}, application.config)


@pytest.mark.parametrize("extra", [
    {"CLOUDFLARE_PROXY_ENABLED": "maybe"},
    {"CLOUDFLARE_ORIGIN_SECRET": "short"},
    {"CLOUDFLARE_ORIGIN_SECRET": "s"*32},
    {"TRUSTED_PROXY_CIDRS": []}, {"TRUSTED_PROXY_CIDRS": ["0.0.0.0/0"]},
    {"TRUSTED_PROXY_CIDRS": ["::/0"]},
])
def test_configuration_fails_closed(application, extra):
    with pytest.raises(ValueError):
        settings({**application.config, "CLOUDFLARE_PROXY_ENABLED": True,
                  "CLOUDFLARE_ORIGIN_SECRET": ORIGIN, "TRUSTED_PROXY_CIDRS": ["10.20.0.0/24"], **extra})
