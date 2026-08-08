"""Runtime configuration loaded from environment variables."""

from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    llm_mode: str = "anthropic"  # "anthropic" | "mock"

    github_mode: str = "mock"
    github_token: str = ""
    github_repo: str = "owner/repo"

    slack_mode: str = "mock"  # "mock" | "webhook" | "bot"
    slack_webhook_url: str = ""
    slack_bot_token: str = ""
    slack_channel: str = "#incidents"

    metrics_mode: str = "mock"
    datadog_api_key: str = ""
    datadog_app_key: str = ""

    runbooks_dir: Path = Path("./runbooks")
    postmortem_dir: Path = Path("./postmortems")
    db_path: Path = Path("./incidents.db")
    environment: Literal["development", "production"] = "development"
    database_url: str = ""
    redis_url: str = ""
    redis_namespace: str = "incident-response"
    redis_require_tls: bool = True
    webhook_token: str = "change-me"

    # Operator identity and browser sessions
    auth_mode: Literal["disabled", "oidc"] = "disabled"
    session_secret: str = ""
    session_seconds: int = 8 * 60 * 60
    session_https_only: bool = True
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_metadata_url: str = ""
    oidc_groups_claim: str = "groups"
    oidc_viewer_groups: str = "incident-viewers"
    oidc_responder_groups: str = "incident-responders"
    oidc_admin_groups: str = "incident-admins"

    # Durable alert queue retry schedule
    queue_retry_base_seconds: float = 1.0
    queue_retry_max_seconds: float = 60.0
    queue_max_attempts: int = 5
    queue_lease_seconds: float = 300.0

    # HMAC signing secrets — optional per source
    datadog_webhook_secret: str = ""
    pagerduty_webhook_secret: str = ""
    generic_webhook_secret: str = ""

    # Rate limiting: per (client-ip + service) sliding window
    rate_limit_max: int = 30
    rate_limit_window_seconds: float = 60.0

    # Dedup
    dedup_bucket_minutes: int = 15
    dedup_ttl_seconds: float = 3600.0

    # Remediation executor
    remediation_mode: str = "mock"  # "mock" | "shell"
    remediation_allowed_commands: str = "feature-flag,kubectl,deploy"  # comma-separated
    remediation_timeout_seconds: float = 30.0

    # Verification loop
    verification_enabled: bool = True
    verification_total_minutes: int = 10
    verification_poll_seconds: int = 30

    # Observability
    log_level: str = "INFO"
    otel_service_name: str = "incident-response"

    @model_validator(mode="after")
    def validate_production_services(self) -> "Settings":
        if self.environment != "production":
            return self
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError(
                "Production requires a PostgreSQL DATABASE_URL using postgresql+psycopg://"
            )
        if not self.redis_url:
            raise ValueError("Production requires a Redis REDIS_URL")
        if self.redis_require_tls and not self.redis_url.startswith("rediss://"):
            raise ValueError("Production Redis requires TLS via rediss://")
        if self.auth_mode != "oidc":
            raise ValueError("Production requires OIDC authentication")
        if len(self.session_secret) < 32:
            raise ValueError("Production session secret must be at least 32 characters")
        if not self.session_https_only:
            raise ValueError("Production requires secure session cookies")
        if not self.oidc_client_id or not self.oidc_client_secret:
            raise ValueError("Production requires OIDC client credentials")
        if not self.oidc_metadata_url.startswith("https://"):
            raise ValueError("Production OIDC metadata URL must use HTTPS")
        return self


def load_settings() -> Settings:
    return Settings()
