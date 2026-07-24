"""Shared deterministic incident scenario for CLI and console demos."""

from datetime import datetime, timezone
from uuid import uuid4

from .models import Alert, Severity

_DEMO_TRIGGERED_AT = datetime(2026, 7, 2, 21, 5, tzinfo=timezone.utc)


def build_demo_alert(
    *,
    alert_id: str = "demo-checkout-001",
    triggered_at: datetime = _DEMO_TRIGGERED_AT,
    metric: str = "http.error_rate",
    source: str | None = None,
) -> Alert:
    """Build the checkout alert used by every local demo entry point."""
    tags = {"env": "demo"}
    if source is not None:
        tags["source"] = source
    return Alert(
        id=alert_id,
        title="Checkout 5xx > 5%",
        description="checkout service error rate at 18%",
        service="checkout",
        severity=Severity.SEV2,
        triggered_at=triggered_at,
        metric=metric,
        threshold=0.05,
        value=0.184,
        tags=tags,
    )


def build_unique_console_demo_alert() -> Alert:
    """Build a collision-safe console alert without changing the demo scenario."""
    token = uuid4().hex
    return build_demo_alert(
        alert_id=f"demo-checkout-{token}",
        triggered_at=datetime.now(timezone.utc),
        metric=f"http.error_rate.demo-{token}",
        source="console",
    )
