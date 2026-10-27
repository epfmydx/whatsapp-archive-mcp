import pytest
from app import create_app
from app.config import settings
from app.proxy import login_source

BASE = "https://archive.test"
PEER = "10.0.1.2"
SECRET = "edge-test-secret-" * 3


def proxy_config(application, mode="signed"):
    return dict(application.config, TRUSTED_PROXY_CIDRS=[PEER+"/32"],
                LOGIN_PROXY_MODE=mode, LOGIN_PROXY_SECRET=SECRET)


def post_login(client, *, source=PEER, headers=None, password="wrong"):
    client.get("/login", base_url=BASE)
    with client.session_transaction(base_url=BASE) as session:
        csrf = session["csrf"]
    return client.post("/login", base_url=BASE, headers=headers or {}, environ_overrides={"REMOTE_ADDR": source},
                       data={"csrf": csrf, "email": "owner@example.invalid", "password": password})


def signed_headers(ip):
    return {"X-Archive-Client-IP": ip, "X-Archive-Proxy-Token": SECRET}


@pytest.mark.parametrize("mode", ["signed", "envoy"])
def test_external_sources_have_independent_budget_behind_one_proxy(application, monkeypatch, mode):
    calls = []
    def password_check(stored, password):
        calls.append(password)
        return password == "correct"
    monkeypatch.setattr("app.auth.check_password_hash", password_check)
    app = create_app(proxy_config(application, mode))
    try:
        headers = lambda ip: signed_headers(ip) if mode == "signed" else {"X-Envoy-External-Address": ip}
        for _ in range(10):
            # A fresh cookie does not grant a fresh password-check budget.
            assert post_login(app.test_client(), headers=headers("198.51.100.4")).status_code == 200
        assert post_login(app.test_client(), headers=headers("198.51.100.4"), password="correct").status_code == 429
        assert len(calls) == 10  # The exhausted source cannot check even a correct password.
        assert post_login(app.test_client(), headers=headers("203.0.113.5"), password="correct").status_code == 302
        assert len(calls) == 11
    finally:
        app.extensions["db_engine"].dispose()


def test_forged_headers_from_untrusted_peer_cannot_select_budget(application, monkeypatch):
    monkeypatch.setattr("app.auth.check_password_hash", lambda *args: False)
    app = create_app(proxy_config(application))
    try:
        for index in range(10):
            headers = signed_headers("198.51.100."+str(index+1))
            headers.update({"X-Forwarded-For": "203.0.113."+str(index+1), "X-Envoy-External-Address": "192.0.2."+str(index+1)})
            assert post_login(app.test_client(), source="10.0.1.3", headers=headers).status_code == 200
        assert post_login(app.test_client(), source="10.0.1.3", headers=signed_headers("192.0.2.20")).status_code == 429
    finally:
        app.extensions["db_engine"].dispose()


@pytest.mark.parametrize("mode", ["signed", "envoy"])
@pytest.mark.parametrize("value", ["", "198.51.100.4, 203.0.113.5", " 198.51.100.4", "not-an-ip", "[2001:db8::1]", "198.51.100.4:80", "fe80::1%eth0"])
def test_malformed_trusted_metadata_fails_before_password_check(application, monkeypatch, mode, value):
    def no_password_check(*args):
        pytest.fail("Unvalidated metadata reached the password hash")
    monkeypatch.setattr("app.auth.check_password_hash", no_password_check)
    app = create_app(proxy_config(application, mode))
    try:
        headers = signed_headers(value) if mode == "signed" else {"X-Envoy-External-Address": value}
        assert post_login(app.test_client(), headers=headers).status_code == 400
    finally:
        app.extensions["db_engine"].dispose()


@pytest.mark.parametrize("token", [None, "wrong", SECRET+", "+SECRET])
def test_missing_or_wrong_edge_token_fails_closed(application, monkeypatch, token):
    monkeypatch.setattr("app.auth.check_password_hash", lambda *args: pytest.fail("Password checked"))
    app = create_app(proxy_config(application))
    try:
        headers = {"X-Archive-Client-IP": "198.51.100.4", "X-Forwarded-For": "203.0.113.5"}
        if token is not None:
            headers["X-Archive-Proxy-Token"] = token
        assert post_login(app.test_client(), headers=headers).status_code == 400
    finally:
        app.extensions["db_engine"].dispose()


@pytest.mark.parametrize("mode", ["", "direct"])
def test_proxy_without_source_mode_fails_closed(application, monkeypatch, mode):
    monkeypatch.setattr("app.auth.check_password_hash", lambda *args: pytest.fail("Password checked"))
    app = create_app(proxy_config(application, mode))
    try:
        assert post_login(app.test_client(), headers=signed_headers("198.51.100.4")).status_code == 400
    finally:
        app.extensions["db_engine"].dispose()


def test_direct_socket_backwards_compatible_and_forwarded_chain_ignored(application):
    app = create_app(dict(application.config, LOGIN_PROXY_MODE="direct", TRUSTED_PROXY_CIDRS=[]))
    try:
        assert post_login(app.test_client(), source="127.0.0.1", headers={"X-Forwarded-For": "spoofed"},
                          password="a-test-password-123456").status_code == 302
    finally:
        app.extensions["db_engine"].dispose()
    config = proxy_config(application)
    for chain in ("203.0.113.5", "forged, 10.0.1.2", "198.51.100.4, 10.0.1.2"):
        environ = {"REMOTE_ADDR": PEER, "HTTP_X_ARCHIVE_CLIENT_IP": "198.51.100.4",
                   "HTTP_X_ARCHIVE_PROXY_TOKEN": SECRET, "HTTP_X_FORWARDED_FOR": chain}
        assert login_source(environ, config) == "198.51.100.4"


def test_source_canonicalizes_ipv6_and_does_not_trust_other_modes(application):
    config = proxy_config(application)
    assert login_source({"REMOTE_ADDR": "::ffff:192.0.2.1"}, config) == "192.0.2.1"
    assert login_source({"REMOTE_ADDR": PEER, "HTTP_X_ARCHIVE_CLIENT_IP": "2001:0db8::1",
                         "HTTP_X_ARCHIVE_PROXY_TOKEN": SECRET}, config) == "2001:db8::1"
    with pytest.raises(ValueError):
        login_source({"REMOTE_ADDR": PEER, "HTTP_X_ENVOY_EXTERNAL_ADDRESS": "192.0.2.1"}, config)
    with pytest.raises(ValueError):
        login_source({"REMOTE_ADDR": ""}, config)


@pytest.mark.parametrize("changes", [{"LOGIN_PROXY_MODE": "unknown"}, {"LOGIN_PROXY_MODE": "signed", "LOGIN_PROXY_SECRET": "short"},
                                     {"LOGIN_PROXY_MODE": "signed", "TRUSTED_PROXY_CIDRS": []}, {"LOGIN_PROXY_MODE": "envoy", "TRUSTED_PROXY_CIDRS": []}])
def test_invalid_proxy_configuration_rejected(application, changes):
    with pytest.raises(ValueError):
        settings(proxy_config(application) | changes)
