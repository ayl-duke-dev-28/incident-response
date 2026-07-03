"""Runtime configuration loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

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
    webhook_token: str = "change-me"

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


def load_settings() -> Settings:
    return Settings()
