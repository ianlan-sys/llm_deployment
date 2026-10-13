from dataclasses import dataclass
from typing import Any

import httpx

from app.providers.base import ProviderUnavailable


UNAVAILABLE_MESSAGE = "本地模型服务暂不可用"
INVALID_RESPONSE_MESSAGE = "本地模型服务响应无效"


@dataclass(frozen=True)
class OllamaHealth:
    available: bool
    version: str | None
    model: str
    installed: bool
    loaded: bool
    size_bytes: int | None


@dataclass(frozen=True)
class OllamaModel:
    name: str
    size_bytes: int | None


@dataclass(frozen=True)
class OllamaRunningModel:
    name: str
    size_bytes: int | None


class OllamaClient:
    """Small, strict client for Ollama's internal health APIs."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ):
        if not base_url.strip() or not model.strip():
            raise ValueError("base_url and model are required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def health(self) -> OllamaHealth:
        return self.model_status()

    def model_status(self) -> OllamaHealth:
        version = self._version()
        tags = self.tags()
        running_models = self.running_models()
        installed_model = next((item for item in tags if item.name == self.model), None)
        return OllamaHealth(
            available=True,
            version=version,
            model=self.model,
            installed=installed_model is not None,
            loaded=any(item.name == self.model for item in running_models),
            size_bytes=installed_model.size_bytes if installed_model else None,
        )

    def tags(self) -> list[OllamaModel]:
        return [
            OllamaModel(name=name, size_bytes=size_bytes)
            for name, size_bytes in self._models("/api/tags")
        ]

    def running_models(self) -> list[OllamaRunningModel]:
        return [
            OllamaRunningModel(name=name, size_bytes=size_bytes)
            for name, size_bytes in self._models("/api/ps")
        ]

    def _version(self) -> str:
        payload = self._get("/api/version")
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            raise ProviderUnavailable(INVALID_RESPONSE_MESSAGE)
        return version

    def _models(self, path: str) -> list[tuple[str, int | None]]:
        models = self._get(path).get("models")
        if not isinstance(models, list):
            raise ProviderUnavailable(INVALID_RESPONSE_MESSAGE)

        parsed: list[tuple[str, int | None]] = []
        for item in models:
            if not isinstance(item, dict):
                raise ProviderUnavailable(INVALID_RESPONSE_MESSAGE)
            name = item.get("name")
            size = item.get("size")
            if not isinstance(name, str) or not name:
                raise ProviderUnavailable(INVALID_RESPONSE_MESSAGE)
            if size is not None and (not isinstance(size, int) or isinstance(size, bool)):
                raise ProviderUnavailable(INVALID_RESPONSE_MESSAGE)
            parsed.append((name, size))
        return parsed

    def _get(self, path: str) -> dict[str, Any]:
        try:
            response = self._client.get(
                f"{self.base_url}{path}", timeout=self.timeout_seconds
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(UNAVAILABLE_MESSAGE) from exc
        except ValueError as exc:
            raise ProviderUnavailable(INVALID_RESPONSE_MESSAGE) from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable(INVALID_RESPONSE_MESSAGE)
        return payload
