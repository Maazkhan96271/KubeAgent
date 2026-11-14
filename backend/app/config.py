"""Application configuration for KubeAgent.

All runtime configuration is loaded from the environment (and an optional
``.env`` file) into a single validated :class:`Settings` object.  Nothing in
the codebase should read ``os.environ`` directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOG_LEVELS: frozenset[str] = frozenset(
    {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
)


class Settings(BaseSettings):
    """Validated application settings.

    Environment variables are case-insensitive, so ``SIMULATION_MODE=true``
    and ``simulation_mode=true`` are equivalent.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_name: str = "KubeAgent"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"

    # ------------------------------------------------------------------
    # Server / CORS
    # ------------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:5173,http://127.0.0.1:5173"
    )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level: str = "INFO"
    uvicorn_log_level: str = "info"
    access_log: bool = True

    # ------------------------------------------------------------------
    # Agent behaviour and safety rails
    # ------------------------------------------------------------------
    #: When true the agent never talks to a real cluster: collectors return
    #: the deterministic scenario from ``collectors/simulator.py``.
    simulation_mode: bool = True
    #: Hard cap on agent reasoning/tool steps per incident.
    agent_max_steps: int = Field(default=6, ge=1, le=32)
    #: Whether the agent may execute remediation commands (off by default).
    auto_remediate: bool = False

    # ------------------------------------------------------------------
    # SRE safety policy (enforced on every remediation approval)
    # ------------------------------------------------------------------
    #: Reject every approval: diagnostics still run, remediation is blocked.
    read_only_mode: bool = False
    #: Namespaces that always require an explicit override to remediate.
    policy_protected_namespaces: str = "production"
    #: Maximum remediation approvals allowed inside the policy window.
    policy_max_approvals_per_window: int = Field(default=2, ge=1, le=50)
    policy_window_seconds: int = Field(default=300, ge=10, le=86400)

    # ------------------------------------------------------------------
    # LLM provider (DeepSeek / OpenAI compatible chat completions API)
    # ------------------------------------------------------------------
    llm_provider: str = "deepseek"
    #: Accept both names so `DEEPSEEK_API_KEY` (used in the docs) and the
    #: provider-neutral `LLM_API_KEY` work without configuration changes.
    llm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY"),
    )
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=2048, ge=128, le=32768)
    llm_timeout_seconds: float = Field(default=45.0, gt=0)

    # ------------------------------------------------------------------
    # Kubernetes
    # ------------------------------------------------------------------
    kubeconfig_path: Path | None = None
    kube_context: str | None = None
    watch_namespaces: str = "default,kube-system"
    k8s_timeout_seconds: int = Field(default=10, ge=1, le=120)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    slack_webhook_url: SecretStr | None = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> str:
        level = str(value or "INFO").strip().upper()
        if level not in LOG_LEVELS:
            allowed = ", ".join(sorted(LOG_LEVELS))
            raise ValueError(f"log_level must be one of: {allowed}")
        return level

    @field_validator("uvicorn_log_level", mode="before")
    @classmethod
    def _normalise_uvicorn_log_level(cls, value: object) -> str:
        return str(value or "info").strip().lower()

    @field_validator("kubeconfig_path", mode="before")
    @classmethod
    def _normalise_kubeconfig(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            return Path(value).expanduser()
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @field_validator("kube_context", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("llm_api_key", "slack_webhook_url", mode="before")
    @classmethod
    def _empty_secret_to_none(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        if isinstance(value, str):
            return value.strip() or None
        return value

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins as a list; ``*`` disables credentials in the app."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def policy_protected_namespace_list(self) -> list[str]:
        """Namespaces that require an explicit override before remediation."""
        return [
            namespace.strip()
            for namespace in self.policy_protected_namespaces.split(",")
            if namespace.strip()
        ]

    @property
    def namespace_list(self) -> list[str]:
        """Namespaces the collectors should watch."""
        return [ns.strip() for ns in self.watch_namespaces.split(",") if ns.strip()]

    @property
    def llm_api_key_value(self) -> str | None:
        """The raw API key, or ``None`` when unset."""
        return self.llm_api_key.get_secret_value() if self.llm_api_key else None

    @property
    def llm_enabled(self) -> bool:
        """True when a usable LLM API key is configured."""
        return bool(self.llm_api_key_value and self.llm_api_key_value.strip())

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
