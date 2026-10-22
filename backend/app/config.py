"""Centralised config. Reads .env from the repo root (or process env).

All secrets come from here — never from module-local constants. The only
hard rule: `AWX_BASE_URL` must stay on the sandbox host for this hackathon.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("closebook.config")

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
    # A model that reads a payment reference is on the CRITICAL PATH of an
    # incoming payment, so the timeout is a real decision. 20s was too short for
    # the configured provider and every call raised ReadTimeout, which read on
    # the page as "the model proposed nothing" — a timeout dressed up as an
    # answer. 45s with the deterministic tiers still deciding either way.
    llm_timeout_s: int = 45
    # Tried when the primary returns a 5xx or times out. A free tier's busy hour
    # must not read as "the model proposed nothing".
    llm_fallback_model: str = ""
    # 429 is a throttle, not a failure: back off and retry the SAME model.
    llm_max_retries: int = 3
    llm_backoff_s: float = 1.5
    # Ask for a JSON object (`response_format`). The transport drops it and
    # retries if the server rejects it, so this is safe on providers that lack it.
    llm_json_mode: bool = True
    # Provider-specific request keys as a JSON object, merged into the body —
    # e.g. NVIDIA NIM's {"chat_template_kwargs": {"enable_thinking": false}} to
    # skip the reasoning pass. Keeping it here means no provider needs a special
    # case in the client.
    llm_extra_body: str = ""

    # Demo: shared PIN gating the approval endpoints.
    demo_approver_pin: SecretStr = Field(default=SecretStr(""))
    # WHO IS DOING THE APPROVING. Audit rows used to be stamped "dashboard",
    # which names a page rather than a person — the one field an auditor actually
    # needs was the one field that could not answer "who decided this?".
    operator_name: str = "david"

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
def _settings_cached() -> Settings:
    return Settings()


_ENV_STAMP: list[float] = [0.0]


def _env_path() -> Path:
    # config.py lives at backend/app/config.py; parents[2] is the repo root
    return Path(__file__).resolve().parents[2] / ".env"


def get_settings() -> Settings:
    """Settings for the CURRENT state of `.env`, not for the moment it started.

    The cache is kept — parsing `.env` on every attribute access is pointless —
    but it is DROPPED the moment the file changes on disk. That is the whole
    difference between "edit your PIN and it works" and "edit your PIN, type it
    correctly, and be told 'That PIN is not right'" because the process has been
    holding the old value since it booted.

    Everything that reads a secret the operator can change mid-session and then
    immediately test must go through here rather than through a module-level
    snapshot taken at import.
    """
    stamp = 0.0
    try:
        stamp = _env_path().stat().st_mtime
    except OSError:
        pass
    if stamp != _ENV_STAMP[0]:
        _ENV_STAMP[0] = stamp
        _settings_cached.cache_clear()
        log.info("settings reloaded: .env changed on disk")
    return _settings_cached()
