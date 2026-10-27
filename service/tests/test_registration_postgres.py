"""Opt-in real row-lock tests, restricted to a disposable local CI database."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app import create_app
from app.config import settings
from app.init_db import initialize
from app.models import Client, DynamicClient, Owner, RateLimit, database
from test_auth import BASE, authorize, exchange, login


@pytest.fixture
def postgres_registration():
    value = os.environ.get("CI_DATABASE_URL")
    if not value:
        pytest.skip("Needs disposable local CI_DATABASE_URL")
    url = make_url(value)
    assert url.drivername == "postgresql+psycopg" and url.host in ("127.0.0.1", "localhost")
    assert url.database in ("whatsapp_mcp_ci", "whatsapp_mcp_dcr_test")
    schema = "dcr_test_"+uuid.uuid4().hex
    control, _ = database(url)
    with control.begin() as db:
        db.execute(text('CREATE SCHEMA "'+schema+'"'))
    isolated_url = url.update_query_dict({"options": "-csearch_path="+schema})
    config = settings({"DATABASE_URL": isolated_url, "TESTING": True, "APP_BASE_URL": BASE,
                       "SECRET_KEY": "s"*32, "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                       "INTERNAL_TOKEN": "i"*32, "ADMIN_PASSWORD": "a-test-password-123456",
                       "OAUTH_CLIENTS": []})
    application = None
    try:
        initialize(config)
        application = create_app(config)
        yield application, config
    finally:
        if application:
            application.extensions["db_engine"].dispose()
        with control.begin() as db:
            db.execute(text('DROP SCHEMA "'+schema+'" CASCADE'))
        control.dispose()


def register_concurrently(application, count=5):
    ready = Barrier(count)
    def attempt(number):
        with application.test_client() as client:
            ready.wait(timeout=10)
            response = client.post("/oauth/register", base_url=BASE, json={
                "client_name": "Synthetic client", "token_endpoint_auth_method": "none",
                "redirect_uris": ["https://client.test/callback/"+str(number)]})
            return response.status_code
    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(attempt, range(count)))


def test_registration_pending_quota_is_serialized_across_connections(postgres_registration, monkeypatch):
    from app import registration
    application, _ = postgres_registration
    monkeypatch.setattr(registration, "MAX_PENDING_CLIENTS", 2)
    results = register_concurrently(application)
    assert sorted(results) == [201, 201, 429, 429, 429]
    with application.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(DynamicClient)))) == 2
        assert db.get(RateLimit, "oauth:dynamic-registration").count == 5


def test_registration_attempt_budget_is_serialized_across_connections(postgres_registration, monkeypatch):
    from app import registration
    application, _ = postgres_registration
    monkeypatch.setattr(registration, "REGISTRATION_ATTEMPTS", 2)
    monkeypatch.setattr(registration, "now", lambda: 1_000_000)
    results = register_concurrently(application)
    assert sorted(results) == [201, 201, 429, 429, 429]
    with application.extensions["db_session"]() as db:
        assert len(list(db.scalars(select(DynamicClient)))) == 2
        assert db.get(RateLimit, "oauth:dynamic-registration").count == 2


def test_additive_bootstrap_preserves_legacy_rows_and_dynamic_registration(postgres_registration):
    application, config = postgres_registration
    sessions = application.extensions["db_session"]
    # Simulate a pre-DCR schema without changing any existing table or row.
    DynamicClient.__table__.drop(application.extensions["db_engine"])
    with sessions.begin() as db:
        owner_hash = db.get(Owner, "owner").password_hash
        db.add(Client(client_id="legacy", name="Existing client", redirect_uris="https://client.test/existing"))
    config = dict(config, OAUTH_CLIENTS=[{"client_id": "legacy", "name": "Existing client",
                                        "redirect_uris": ["https://client.test/existing"]}])
    initialize(config)
    client = application.test_client()
    registered = client.post("/oauth/register", base_url=BASE, json={"redirect_uris": ["https://client.test/new"]})
    assert registered.status_code == 201
    client_id = registered.json["client_id"]
    initialize(config)
    initialize(config)
    with sessions() as db:
        assert db.get(Owner, "owner").password_hash == owner_hash
        assert db.get(Client, "legacy").redirect_uris == "https://client.test/existing"
        assert db.get(Client, client_id).enabled
        assert db.get(DynamicClient, client_id)


def test_concurrent_refresh_and_owner_disconnect_leave_no_usable_grants(postgres_registration):
    application, _ = postgres_registration
    owner = application.test_client()
    cid = owner.post("/oauth/register", base_url=BASE, json={
        "redirect_uris": ["https://client.test/callback"]}).json["client_id"]
    issued = exchange(owner, authorize(owner, client_id=cid), client_id=cid).json
    login(owner)
    owner.get("/connections", base_url=BASE)
    with owner.session_transaction(base_url=BASE) as session:
        csrf = session["csrf"]
    ready = Barrier(2)
    def refresh():
        with application.test_client() as client:
            ready.wait(timeout=10)
            return client.post("/oauth/token", base_url=BASE, data={
                "grant_type": "refresh_token", "client_id": cid,
                "refresh_token": issued["refresh_token"], "resource": BASE+"/mcp"})
    def disconnect():
        ready.wait(timeout=10)
        return owner.post("/connections/"+cid+"/disconnect", base_url=BASE, data={"csrf": csrf})
    with ThreadPoolExecutor(max_workers=2) as executor:
        refreshed, disconnected = executor.submit(refresh), executor.submit(disconnect)
        refreshed, disconnected = refreshed.result(timeout=10), disconnected.result(timeout=10)
    assert refreshed.status_code in (200, 400) and disconnected.status_code == 303
    reader = application.test_client()
    for access in [issued["access_token"], *([refreshed.json["access_token"]] if refreshed.status_code == 200 else [])]:
        assert reader.post("/mcp", base_url=BASE, headers={"Authorization": "Bearer "+access},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
