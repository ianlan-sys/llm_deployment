from dataclasses import dataclass
from threading import RLock
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FeatureProviderSetting
from app.providers.base import AIProvider, ProviderUnavailable
from app.providers.mock import MockProvider
from app.providers.ollama import OllamaClient
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.schemas import ProviderHealth


FeatureKey = Literal["call_assistant", "knowledge", "document_review"]
FeatureMode = Literal["mock", "local"]

FEATURES: tuple[FeatureKey, ...] = (
    "call_assistant",
    "knowledge",
    "document_review",
)
DEFAULT_MODES: dict[FeatureKey, FeatureMode] = {
    "call_assistant": "mock",
    "knowledge": "local",
    "document_review": "mock",
}
LOCAL_UNAVAILABLE_DETAIL = "本地模型不可用，请手动将该业务切换为 Mock 模式后重试"
LOCAL_AVAILABLE_DETAIL = "本地模型可用"


@dataclass(frozen=True)
class FeatureProviderSnapshot:
    """An immutable provider view for one independently configured feature."""

    feature: FeatureKey
    mode: FeatureMode
    model: str | None
    provider: AIProvider


class FeatureProviderRouter:
    """Publishes per-feature provider snapshots only after durable commits."""

    def __init__(self, _bind: object, ollama_client: OllamaClient):
        self._ollama_client = ollama_client
        self._lock = RLock()
        self._snapshots = self._build_snapshots({})

    def snapshot(self, feature: FeatureKey) -> FeatureProviderSnapshot:
        self._validate_feature(feature)
        with self._lock:
            return self._snapshots[feature]

    def snapshots(self) -> tuple[FeatureProviderSnapshot, ...]:
        with self._lock:
            return tuple(self._snapshots[feature] for feature in FEATURES)

    def load(self, session: Session) -> None:
        settings = {
            setting.feature: setting
            for setting in session.scalars(select(FeatureProviderSetting))
            if setting.feature in FEATURES
        }
        desired = self._build_snapshots(settings)
        with self._lock:
            self._snapshots = desired

    def configure_and_persist(
        self,
        session: Session,
        feature: FeatureKey,
        mode: FeatureMode,
        model: str | None = None,
    ) -> FeatureProviderSnapshot:
        self._validate_feature(feature)
        self._validate_mode(mode)
        current = self.snapshot(feature)
        desired_model = model.strip() if model else current.model or self._default_model()
        if mode == "local":
            self._validate_local_model(desired_model)
        desired = self._build_snapshot(feature, mode, desired_model)

        with self._lock:
            setting = session.scalar(
                select(FeatureProviderSetting).where(
                    FeatureProviderSetting.feature == feature
                )
            )
            if setting is None:
                session.add(
                    FeatureProviderSetting(
                        feature=feature,
                        mode=desired.mode,
                        model=desired.model or "",
                    )
                )
            else:
                setting.mode = desired.mode
                setting.model = desired.model or ""
            try:
                session.commit()
            except Exception:
                session.rollback()
                raise
            self._snapshots = self._snapshots | {feature: desired}
        return desired

    def test(
        self, feature: FeatureKey, mode: FeatureMode, model: str | None = None
    ) -> ProviderHealth:
        self._validate_feature(feature)
        self._validate_mode(mode)
        if mode == "mock":
            return MockProvider().health_check()
        current = self.snapshot(feature)
        desired_model = model.strip() if model else current.model or self._default_model()
        try:
            self._validate_local_model(desired_model)
        except ProviderUnavailable:
            return ProviderHealth(available=False, detail=LOCAL_UNAVAILABLE_DETAIL)
        return ProviderHealth(available=True, detail=LOCAL_AVAILABLE_DETAIL)

    def _build_snapshots(self, settings: dict[str, FeatureProviderSetting]) -> dict[FeatureKey, FeatureProviderSnapshot]:
        return {
            feature: self._build_snapshot(
                feature,
                self._setting_mode(settings.get(feature), feature),
                self._setting_model(settings.get(feature)),
            )
            for feature in FEATURES
        }

    def _setting_mode(
        self, setting: FeatureProviderSetting | None, feature: FeatureKey
    ) -> FeatureMode:
        return DEFAULT_MODES[feature] if setting is None else self._coerce_mode(setting.mode)

    def _setting_model(self, setting: FeatureProviderSetting | None) -> str:
        return (
            self._default_model()
            if setting is None or not setting.model
            else setting.model
        )

    def _build_snapshot(
        self, feature: FeatureKey, mode: FeatureMode, model: str
    ) -> FeatureProviderSnapshot:
        if mode == "mock":
            return FeatureProviderSnapshot(
                feature=feature,
                mode=mode,
                model=None,
                provider=MockProvider(),
            )
        return FeatureProviderSnapshot(
            feature=feature,
            mode=mode,
            model=model,
            provider=OpenAICompatibleProvider(
                base_url=f"{self._ollama_base_url()}/v1",
                model=model,
                timeout_seconds=self._ollama_timeout_seconds(),
            ),
        )

    def _validate_local_model(self, model: str | None) -> None:
        if not model:
            raise ProviderUnavailable(LOCAL_UNAVAILABLE_DETAIL)
        try:
            installed = any(item.name == model for item in self._ollama_client.tags())
        except ProviderUnavailable as exc:
            raise ProviderUnavailable(LOCAL_UNAVAILABLE_DETAIL) from exc
        if not installed:
            raise ProviderUnavailable(LOCAL_UNAVAILABLE_DETAIL)

    def _default_model(self) -> str:
        return getattr(self._ollama_client, "model", "unconfigured-local-model")

    def _ollama_base_url(self) -> str:
        return getattr(self._ollama_client, "base_url", "http://ollama:11434")

    def _ollama_timeout_seconds(self) -> float:
        return getattr(self._ollama_client, "timeout_seconds", 5.0)

    @staticmethod
    def _validate_feature(feature: str) -> None:
        if feature not in FEATURES:
            raise ValueError("unknown feature")

    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode not in {"mock", "local"}:
            raise ValueError("mode must be mock or local")

    @classmethod
    def _coerce_mode(cls, mode: str) -> FeatureMode:
        cls._validate_mode(mode)
        return mode  # type: ignore[return-value]
