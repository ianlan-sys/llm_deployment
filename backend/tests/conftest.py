"""Shared pytest configuration for the backend test suite."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth import encode_scrypt_hash
from app.config import Settings
from app.main import create_app
from app.models import Base

@pytest.fixture(autouse=True)
def disable_auth_by_default(monkeypatch):
    """Keep pre-auth API tests compatible with the local test server."""
    monkeypatch.setenv("DEMO_AUTH_REQUIRED", "false")


@pytest.fixture
def auth_client(tmp_path):
    settings = Settings(
        runtime_dir=tmp_path,
        auth_required=True,
        auth_username="demo",
        auth_password_hash=encode_scrypt_hash("correct horse"),
        session_secret="test-session-secret",
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()
