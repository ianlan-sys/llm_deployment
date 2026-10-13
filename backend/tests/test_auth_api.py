import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from pydantic import SecretStr
from sqlalchemy import select

from app.auth import hash_token
from app.models import AuditEvent, AuthSession, LoginAttempt


def test_business_api_requires_session_and_login_sets_http_only_cookie(auth_client):
    unauthenticated = auth_client.get("/api/dashboard")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "\u8bf7\u5148\u767b\u5f55"}
    response = auth_client.post(
        "/api/auth/login", json={"username": "demo", "password": "correct horse"}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=28800" in cookie
    assert auth_client.get("/api/dashboard").status_code == 200


def test_sixth_failed_login_is_rate_limited(auth_client):
    for _ in range(5):
        failed_login = auth_client.post(
            "/api/auth/login", json={"username": "demo", "password": "wrong"}
        )
        assert failed_login.status_code == 401
        assert failed_login.json() == {"detail": "\u7528\u6237\u540d\u6216\u5bc6\u7801\u9519\u8bef"}
    rate_limited = auth_client.post(
        "/api/auth/login", json={"username": "demo", "password": "wrong"}
    )
    assert rate_limited.status_code == 429
    assert rate_limited.json() == {"detail": "\u767b\u5f55\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41"}


def test_session_and_logout_revoke_the_server_side_session(auth_client):
    assert auth_client.post("/api/auth/logout").status_code == 401
    assert auth_client.get("/api/auth/session").status_code == 401

    assert auth_client.post(
        "/api/auth/login", json={"username": "demo", "password": "correct horse"}
    ).status_code == 200
    assert auth_client.get("/api/auth/session").json() == {"username": "demo"}
    assert auth_client.post("/api/auth/logout").status_code == 204
    assert auth_client.get("/api/auth/session").status_code == 401
    with auth_client.app.state.session_factory() as session:
        assert list(session.scalars(select(AuthSession))) == []


def test_rotating_session_secret_invalidates_existing_cookie(auth_client):
    assert auth_client.post(
        "/api/auth/login", json={"username": "demo", "password": "correct horse"}
    ).status_code == 200
    raw_token = auth_client.cookies.get("demo_session")
    assert raw_token

    with auth_client.app.state.session_factory() as session:
        stored = session.scalar(select(AuthSession))
        assert stored is not None
        assert stored.token_hash == hash_token(raw_token, "test-session-secret")
        assert stored.token_hash != hash_token(raw_token, "rotated-session-secret")
        assert raw_token not in stored.token_hash
        assert "test-session-secret" not in stored.token_hash

    auth_client.app.state.settings.session_secret = SecretStr(
        "rotated-session-secret"
    )

    response = auth_client.get("/api/auth/session")
    assert response.status_code == 401
    assert response.json() == {"detail": "\u8bf7\u5148\u767b\u5f55"}


def test_authenticated_mutation_rejects_cross_origin_request(auth_client):
    assert auth_client.post(
        "/api/auth/login", json={"username": "demo", "password": "correct horse"}
    ).status_code == 200

    response = auth_client.post(
        "/api/settings/provider/test",
        json={"mode": "demo"},
        headers={"Origin": "http://attacker.invalid"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "\u975e\u6cd5\u6765\u6e90\u8bf7\u6c42"}


def test_auth_audits_are_fixed_redacted_summaries(auth_client):
    username = "demo"
    password = "wrong-password"
    assert auth_client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).status_code == 401

    with auth_client.app.state.session_factory() as session:
        summaries = [
            event.request_summary
            for event in session.scalars(select(AuditEvent).order_by(AuditEvent.id))
        ]

    serialized = json.dumps(summaries)
    assert username not in serialized
    assert password not in serialized


def test_unicode_username_is_rejected_and_recorded_redacted(auth_client):
    response = auth_client.post(
        "/api/auth/login",
        json={"username": "\u7528\u6237", "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "\u7528\u6237\u540d\u6216\u5bc6\u7801\u9519\u8bef"}
    with auth_client.app.state.session_factory() as session:
        attempt = session.scalar(select(LoginAttempt))
    assert attempt is not None
    assert attempt.username == "redacted"
    assert attempt.succeeded is False


def test_concurrent_failed_logins_enforce_the_five_attempt_quota(auth_client):
    attempts = 8
    barrier = Barrier(attempts)

    def failed_login() -> int:
        barrier.wait()
        return auth_client.post(
            "/api/auth/login", json={"username": "demo", "password": "wrong"}
        ).status_code

    with ThreadPoolExecutor(max_workers=attempts) as executor:
        statuses = list(executor.map(lambda _: failed_login(), range(attempts)))

    assert statuses.count(401) == 5
    assert statuses.count(429) == attempts - 5
