from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEMO_", extra="ignore")

    runtime_dir: Path = Path("data/runtime")
    database_url: str | None = None
    provider_mode: str = "demo"
    openai_base_url: str | None = None
    openai_model: str | None = None
    openai_api_key: SecretStr | None = Field(default=None, repr=False)
    provider_timeout_seconds: float = 30.0
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:1.7b-q4_K_M"
    ollama_timeout_seconds: float = 5.0
    max_upload_bytes: int = 20 * 1024 * 1024
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]
    auth_required: bool = True
    auth_username: str = ""
    auth_password_hash: SecretStr = Field(default=SecretStr(""), repr=False)
    session_secret: SecretStr = Field(default=SecretStr(""), repr=False)

    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.runtime_dir / 'demo.db'}"
