import pytest
from cryptography.fernet import Fernet
from app import create_app
from app.init_db import initialize
from app.config import settings

@pytest.fixture
def application(tmp_path):
    c = settings({"DATABASE_URL": "sqlite:///"+str(tmp_path/"test.db"), "APP_BASE_URL": "https://archive.test", "SECRET_KEY": "s"*32,
                  "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(), "INTERNAL_TOKEN": "i"*32, "ADMIN_PASSWORD": "a-test-password-123456",
                  "OAUTH_CLIENTS": [{"client_id": "desktop", "name": "Desktop", "redirect_uris": ["https://client.test/callback"]}],
                  "TESTING": True})
    initialize(c)
    app = create_app(c)
    yield app
    app.extensions["db_engine"].dispose()

@pytest.fixture
def client(application): return application.test_client()

@pytest.fixture
def internal(): return {"Authorization": "Bearer "+"i"*32}
