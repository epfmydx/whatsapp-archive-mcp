import pytest
from flask import request, session

from test_auth import BASE, login


def public_asset(response):
    assert "Cache-Control" not in response.headers
    assert "Expires" not in response.headers
    assert "Set-Cookie" not in response.headers
    assert "Cookie" not in response.vary
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("authenticated", [False, True])
def test_static_get_head_and_conditional_responses_leave_cache_policy_to_edge(client, authenticated):
    if authenticated:
        login(client)
    response = client.get("/static/app.css", base_url=BASE)
    assert response.status_code == 200 and response.mimetype == "text/css"
    assert response.headers["ETag"] and response.headers["Last-Modified"]
    public_asset(response)
    head = client.head("/static/app.css", base_url=BASE)
    assert head.status_code == 200 and not head.data
    public_asset(head)
    conditional = client.get("/static/app.css", base_url=BASE, headers={"If-None-Match": response.headers["ETag"]})
    assert conditional.status_code == 304 and not conditional.data
    public_asset(conditional)
    if authenticated:
        private = client.get("/profiles/personal", base_url=BASE)
        assert private.status_code == 200
        assert private.headers["Cache-Control"] == "no-store"
        assert "Set-Cookie" in private.headers  # Normal private-session refresh remains.


@pytest.mark.parametrize("method,path,status", [
    ("get", "/login", 200), ("get", "/", 302), ("get", "/profiles/new", 302),
    ("get", "/oauth/authorize", 302), ("post", "/mcp", 401),
    ("post", "/oauth/token", 400), ("get", "/accounts/personal/qr.png", 302),
    ("get", "/static/missing.css", 404), ("get", "/static/../app/config.py", 404),
    ("post", "/static/app.css", 405), ("get", "/login?asset=/static/app.css", 200),
])
def test_private_responses_and_static_errors_remain_no_store(client, method, path, status):
    response = getattr(client, method)(path, base_url=BASE)
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"


def test_early_response_on_static_endpoint_cannot_become_public(application, client):
    @application.before_request
    def intercepted():
        if request.endpoint == "static":
            return "Synthetic private response", 200
    response = client.get("/static/app.css", base_url=BASE)
    assert response.headers["Cache-Control"] == "no-store"


def test_static_request_that_changes_session_fails_closed(application, client):
    @application.before_request
    def modifies_session():
        if request.endpoint == "static":
            session["synthetic"] = "changed"
    response = client.get("/static/app.css", base_url=BASE)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" in response.headers
