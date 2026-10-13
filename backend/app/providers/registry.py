from dataclasses import dataclass
from threading import RLock

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting
from app.providers.base import AIProvider, ProviderUnavailable
from app.providers.mock import MockProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas import ProviderHealth, ProviderSettingsResponse


@dataclass(frozen=True)
class ProviderSnapshot:
    """One immutable view of provider identity and its matching metadata."""

    mode: str
    base_url: str | None
    model: str | None
    provider: AIProvider | None

    def require_provider(self) -> AIProvider:
        if self.provider is None:
            raise ProviderUnavailable("Real provider requires a base URL and model")
        return self.provider


class ProviderRegistry:
    """Legacy global registry retained until workflow execution adopts feature routing.

    New settings are owned by ``FeatureProviderRouter`` so each feature can publish an
    independent immutable snapshot. Existing workflows continue to consume this
    registry during the staged migration.
    """

    def __init__(
        self,
        mode: str = "demo",
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ):
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._lock = RLock()
        self._snapshot = self._build_snapshot(mode, base_url, model)

    @property
    def api_key(self) -> str | None:
        """Exposed for process-local use only; API schemas intentionally omit it."""
        return self._api_key

    @property
    def mode(self) -> str:
        return self.snapshot().mode

    @property
    def base_url(self) -> str | None:
        return self.snapshot().base_url

    @property
    def model(self) -> str | None:
        return self.snapshot().model

    def snapshot(self) -> ProviderSnapshot:
        with self._lock:
            return self._snapshot

    def settings_response(self) -> ProviderSettingsResponse:
        snapshot = self.snapshot()
        return ProviderSettingsResponse(
            mode=snapshot.mode,
            base_url=snapshot.base_url,
            model=snapshot.model,
        )

    def load_from_settings(self, session: Session) -> None:
        values = {row.key: row.value for row in session.scalars(select(AppSetting))}
        current = self.snapshot()
        desired = self._build_snapshot(
            values.get("provider_mode", current.mode),
            values.get("provider_base_url", current.base_url) or None,
            values.get("provider_model", current.model) or None,
        )
        with self._lock:
            self._snapshot = desired

    def configure_and_persist(
        self,
        session: Session,
        mode: str,
        base_url: str | None,
        model: str | None,
    ) -> ProviderSnapshot:
        desired = self._build_snapshot(mode, base_url, model)
        values = {
            "provider_mode": desired.mode,
            "provider_base_url": desired.base_url or "",
            "provider_model": desired.model or "",
        }
        # Serialize concurrent writers so commit order and published order cannot diverge.
        with self._lock:
            existing = {
                row.key: row
                for row in session.scalars(
                    select(AppSetting).where(AppSetting.key.in_(values))
                )
            }
            for key, value in values.items():
                if key in existing:
                    existing[key].value = value
                else:
                    session.add(AppSetting(key=key, value=value))
            try:
                session.commit()
            except Exception:
                session.rollback()
                raise
            self._snapshot = desired
            return desired

    def get_active(self) -> AIProvider:
        """Compatibility helper; consumers should acquire one snapshot per operation."""
        return self.snapshot().require_provider()

    def test(
        self,
        mode: str,
        base_url: str | None,
        model: str | None,
        api_key: str | None,
    ) -> ProviderHealth:
        if mode == "demo":
            return MockProvider().health_check()
        try:
            if not base_url or not model:
                raise ProviderUnavailable("Real provider requires a base URL and model")
            return OpenAICompatibleProvider(
                base_url=base_url,
                model=model,
                api_key=api_key if api_key is not None else self._api_key,
                timeout_seconds=self.timeout_seconds,
            ).health_check()
        except (ProviderUnavailable, ValueError):
            return ProviderHealth(available=False, detail="模型连接不可用")

    def _build_snapshot(
        self, mode: str, base_url: str | None, model: str | None
    ) -> ProviderSnapshot:
        if mode not in {"demo", "real"}:
            raise ValueError("mode must be demo or real")
        normalized_url = base_url or None
        normalized_model = model or None
        provider: AIProvider | None
        if mode == "demo":
            provider = MockProvider()
        elif normalized_url and normalized_model:
            provider = OpenAICompatibleProvider(
                base_url=normalized_url,
                model=normalized_model,
                api_key=self._api_key,
                timeout_seconds=self.timeout_seconds,
            )
        else:
            provider = None
        return ProviderSnapshot(
            mode=mode,
            base_url=normalized_url,
            model=normalized_model,
            provider=provider,
        )
