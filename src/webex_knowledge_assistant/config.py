from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    """Raised when a runtime role is unsafe to start."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./data/assistant.db"
    knowledge_manifest: Path = Path("examples/knowledge/sample-manifest.json")
    allowed_webex_space_ids: str = ""
    allowed_webex_person_ids: str = ""
    allow_direct_messages: bool = False
    require_mention: bool = True
    webex_bot_person_id: str = ""
    webex_bot_token: str = ""
    webex_webhook_secret: str = ""
    webex_api_base_url: str = "https://webexapis.com/v1"
    retrieval_min_score: float = Field(default=0.34, ge=0.0, le=1.0)
    worker_poll_seconds: float = Field(default=1.0, gt=0.0, le=60.0)
    job_lease_seconds: int = Field(default=60, ge=10, le=3600)
    job_max_attempts: int = Field(default=3, ge=1, le=20)

    @property
    def allowed_space_ids(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.allowed_webex_space_ids.split(",") if item.strip()
        )

    @property
    def allowed_person_ids(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.allowed_webex_person_ids.split(",") if item.strip()
        )

    def validate_for_role(self, role: Literal["api", "worker", "migration"]) -> None:
        if not self.database_url.strip():
            raise ConfigurationError("DATABASE_URL is required")
        if self.environment == "production" and self.database_url.startswith("sqlite"):
            raise ConfigurationError("Production requires a shared durable database")
        if role == "migration":
            return
        if not self.knowledge_manifest.is_file():
            raise ConfigurationError(
                f"KNOWLEDGE_MANIFEST does not exist: {self.knowledge_manifest}"
            )
        if self.environment != "production":
            return
        if role == "api":
            if not self.webex_webhook_secret:
                raise ConfigurationError("WEBEX_WEBHOOK_SECRET is required in production")
            return
        api_origin = urlsplit(self.webex_api_base_url)
        if (
            api_origin.scheme != "https"
            or api_origin.hostname != "webexapis.com"
            or api_origin.port not in (None, 443)
            or api_origin.username
            or api_origin.password
            or api_origin.query
            or api_origin.fragment
            or api_origin.path.rstrip("/") != "/v1"
        ):
            raise ConfigurationError(
                "Production WEBEX_API_BASE_URL must be https://webexapis.com/v1"
            )
        if self.allow_direct_messages and not self.allowed_person_ids:
            raise ConfigurationError(
                "Direct messages require an exact ALLOWED_WEBEX_PERSON_IDS policy"
            )
        if not self.allowed_space_ids and not (
            self.allow_direct_messages and self.allowed_person_ids
        ):
            raise ConfigurationError(
                "Production requires ALLOWED_WEBEX_SPACE_IDS or an explicit direct-message policy"
            )
        missing = [
            name
            for name, value in (
                ("WEBEX_BOT_PERSON_ID", self.webex_bot_person_id),
                ("WEBEX_BOT_TOKEN", self.webex_bot_token),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(f"Worker configuration is missing: {', '.join(missing)}")
