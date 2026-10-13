"""Server-side authentication for the short-lived demo account."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import secrets
from threading import Lock

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditEvent, AuthSession, LoginAttempt

SESSION_TTL_SECONDS = 8 * 60 * 60
LOGIN_WINDOW = timedelta(minutes=10)
MAX_FAILED_LOGINS = 5

_REDACTED_LOGIN_IDENTITY = "redacted"
_LOGIN_SUCCEEDED = "Demo login succeeded"
_LOGIN_FAILED = "Demo login failed"
_LOGIN_RATE_LIMITED = "Demo login rate limited"
_LOGOUT_SUCCEEDED = "Demo logout succeeded"
_LOGIN_LOCK = Lock()


@dataclass(frozen=True)
class AuthPrincipal:
    username: str


@dataclass(frozen=True)
class LoginResult:
    principal: AuthPrincipal | None = None
    raw_token: str | None = None
    rate_limited: bool = False

    @property
    def succeeded(self) -> bool:
        return self.principal is not None and self.raw_token is not None


def encode_scrypt_hash(password: str, salt: bytes | None = None) -> str:
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=actual_salt, n=2**14, r=8, p=1, dklen=32
    )
    return f"scrypt$16384$8$1${actual_salt.hex()}${digest.hex()}"


def verify_scrypt_hash(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (TypeError, ValueError):
        return False


class AuthService:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings

    def login(self, username: str, password: str, client_ip: str) -> LoginResult:
        with _LOGIN_LOCK:
            return self._login(username, password, client_ip)

    def _login(self, username: str, password: str, client_ip: str) -> LoginResult:
        normalized_ip = normalize_client_ip(client_ip)
        now = datetime.now(timezone.utc)
        failed_attempts = self.session.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.client_ip == normalized_ip,
                LoginAttempt.succeeded.is_(False),
                LoginAttempt.created_at >= now - LOGIN_WINDOW,
            )
        )
        if failed_attempts >= MAX_FAILED_LOGINS:
            self._record_audit(_LOGIN_RATE_LIMITED, "rate_limited")
            self.session.commit()
            return LoginResult(rate_limited=True)

        password_hash = self.settings.auth_password_hash.get_secret_value()
        authenticated = bool(
            self.settings.auth_username
            and hmac.compare_digest(
                username.encode("utf-8"), self.settings.auth_username.encode("utf-8")
            )
            and verify_scrypt_hash(password, password_hash)
        )
        self.session.add(
            LoginAttempt(
                username=_REDACTED_LOGIN_IDENTITY,
                client_ip=normalized_ip,
                succeeded=authenticated,
            )
        )
        if not authenticated:
            self._record_audit(_LOGIN_FAILED, "unauthorized")
            self.session.commit()
            return LoginResult()

        raw_token = secrets.token_urlsafe(32)
        self.session.add(
            AuthSession(
                username=self.settings.auth_username,
                token_hash=self._hash_token(raw_token),
                expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
            )
        )
        self._record_audit(_LOGIN_SUCCEEDED, "completed")
        self.session.commit()
        return LoginResult(
            principal=AuthPrincipal(username=self.settings.auth_username),
            raw_token=raw_token,
        )

    def authenticate(self, raw_token: str | None) -> AuthPrincipal | None:
        if not raw_token:
            return None
        session = self.session.scalar(
            select(AuthSession).where(
                AuthSession.token_hash == self._hash_token(raw_token)
            )
        )
        if session is None:
            return None
        if _as_utc(session.expires_at) <= datetime.now(timezone.utc):
            self.session.delete(session)
            self.session.commit()
            return None
        return AuthPrincipal(username=session.username)

    def logout(self, raw_token: str | None) -> None:
        if raw_token:
            self.session.execute(
                delete(AuthSession).where(
                    AuthSession.token_hash == self._hash_token(raw_token)
                )
            )
        self._record_audit(_LOGOUT_SUCCEEDED, "completed")
        self.session.commit()

    def _record_audit(self, request_summary: str, result_status: str) -> None:
        self.session.add(
            AuditEvent(
                event_type="auth.session",
                request_summary=request_summary,
                mode="demo",
                result_status=result_status,
            )
        )

    def _hash_token(self, raw_token: str) -> str:
        return hash_token(
            raw_token, self.settings.session_secret.get_secret_value()
        )


def hash_token(raw_token: str, session_secret: str) -> str:
    return hmac.new(
        session_secret.encode("utf-8"),
        raw_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def normalize_client_ip(value: str | None) -> str:
    try:
        return ipaddress.ip_address(value or "").compressed
    except ValueError:
        return "unknown"


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
