"""Centralised config. Reads .env from the repo root (or process env).

All secrets come from here — never from module-local constants. The only
hard rule: `AWX_BASE_URL` must stay on the sandbox host for this hackathon.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py lives at backend/app/config.py; parents[2] is the repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

ALLOWED_AWX_HOSTS = {"api.sandbox.airwallex.com", "sandbox.airwallex.com"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    # Airwallex sandbox credentials
    awx_client_id: SecretStr = Field(default=SecretStr(""), description="sandbox Client ID")
    awx_api_key: SecretStr = Field(default=SecretStr(""), description="sandbox API key")
    awx_webhook_secret: SecretStr = Field(default=SecretStr(""), description="per-URL webhook HMAC secret")
    awx_base_url: str = Field(default="https://api.sandbox.airwallex.com")

    @field_validator("awx_base_url")
    @classmethod
    def _sandbox_only(cls, v: str) -> str:
        host = v.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if host not in ALLOWED_AWX_HOSTS:
            raise ValueError(f"AWX_BASE_URL must be the Airwallex sandbox ({host} is not allowed)")
        return v

    # LLM — OpenAI-compatible endpoint. Optional: with no key the deterministic
    # tiers carry everything and `propose()` returns None.
    llm_api_key: SecretStr = Field(default=SecretStr(""))
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # Demo: shared PIN gating the approval endpoints.
    demo_approver_pin: SecretStr = Field(default=SecretStr(""))

    # App
    closebook_hmac_secret: SecretStr = Field(default=SecretStr(""))
    public_base_url: str = "http://localhost:8000"
    webhook_path: str = "/webhooks/airwallex"
    log_level: str = "INFO"
    db_path: str = str(REPO_ROOT / "backend" / "data" / "closebook.sqlite3")

    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.webhook_path}"

    def sandbox_ready(self) -> bool:
        """True when the two credentials needed for Phase 1 are present."""
        return bool(
            self.awx_client_id.get_secret_value().strip()
            and self.awx_api_key.get_secret_value().strip()
        )

    def llm_ready(self) -> bool:
        """True when an OpenAI-compatible LLM endpoint is configured.

        Deliberately NOT required: Closebook is fully deterministic without it.
        """
        return bool(self.llm_api_key.get_secret_value().strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
