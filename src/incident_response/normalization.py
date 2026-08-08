"""Provider webhook payloads normalized into the shared alert schema."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .models import Alert, Severity


def _text(value: object, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("Alert payload is missing a valid timestamp")


def _tags(value: object) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(key): str(item) for key, item in value.items()}
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        return {}
    result: dict[str, str] = {}
    for item in values:
        key, separator, tag_value = str(item).strip().partition(":")
        if separator and key:
            result[key] = tag_value
    return result


def correlation_key(
    *,
    provider_incident_key: str | None,
    explicit: str,
    service: str,
    metric: str | None,
    environment: str,
) -> str:
    return (
        provider_incident_key
        or explicit
        or f"{service}|{metric or ''}|{environment or ''}"
    )


def _severity(value: object, *, urgency: str = "") -> Severity:
    normalized = _text(value).lower()
    aliases = {
        "p1": Severity.SEV1,
        "critical": Severity.SEV1,
        "sev1": Severity.SEV1,
        "p2": Severity.SEV2,
        "error": Severity.SEV2,
        "high": Severity.SEV2,
        "sev2": Severity.SEV2,
        "p3": Severity.SEV3,
        "warning": Severity.SEV3,
        "warn": Severity.SEV3,
        "sev3": Severity.SEV3,
        "p4": Severity.SEV4,
        "info": Severity.SEV4,
        "low": Severity.SEV4,
        "sev4": Severity.SEV4,
    }
    return aliases.get(normalized, Severity.SEV2 if urgency == "high" else Severity.SEV3)


def normalize_generic_alert(payload: Mapping[str, Any]) -> Alert:
    event_id = _text(payload.get("provider_event_id")) or _text(payload.get("id"))
    if not event_id:
        raise ValueError("Generic alert is missing id")
    service = _text(payload.get("service"))
    if not service:
        raise ValueError("Generic alert is missing service")
    metric = _text(payload.get("metric")) or None
    environment = _text(payload.get("environment"))
    provider_key = _text(payload.get("provider_incident_key")) or None
    return Alert(
        id=event_id,
        source=_text(payload.get("source"), "generic"),
        provider_event_id=event_id,
        provider_incident_key=provider_key,
        correlation_key=correlation_key(
            provider_incident_key=provider_key,
            explicit=_text(payload.get("correlation_key")),
            service=service,
            metric=metric,
            environment=environment,
        ),
        title=_text(payload.get("title"), "Untitled alert"),
        description=_text(payload.get("description")),
        service=service,
        severity=_severity(payload.get("severity")),
        triggered_at=_timestamp(payload.get("triggered_at")),
        metric=metric,
        environment=environment,
        threshold=payload.get("threshold"),
        value=payload.get("value"),
        tags=_tags(payload.get("tags")),
        raw=dict(payload),
    )


def normalize_datadog_alert(payload: Mapping[str, Any]) -> Alert:
    tags = _tags(payload.get("tags"))
    event_id = _text(payload.get("id")) or _text(payload.get("event_id"))
    if not event_id:
        raise ValueError("Datadog alert is missing event id")
    service = _text(payload.get("service")) or tags.get("service", "")
    if not service:
        raise ValueError("Datadog alert is missing service")
    metric = _text(payload.get("metric")) or None
    environment = _text(payload.get("environment")) or tags.get("env", "")
    provider_key = _text(payload.get("aggregation_key")) or None
    return Alert(
        id=f"datadog:{event_id}",
        source="datadog",
        provider_event_id=event_id,
        provider_incident_key=provider_key,
        correlation_key=correlation_key(
            provider_incident_key=provider_key,
            explicit=_text(payload.get("correlation_key")),
            service=service,
            metric=metric,
            environment=environment,
        ),
        title=_text(payload.get("title"), "Datadog alert"),
        description=_text(payload.get("body")) or _text(payload.get("description")),
        service=service,
        severity=_severity(payload.get("priority") or payload.get("alert_type")),
        triggered_at=_timestamp(payload.get("date") or payload.get("triggered_at")),
        metric=metric,
        environment=environment,
        tags=tags,
        raw=dict(payload),
    )


def normalize_pagerduty_alert(payload: Mapping[str, Any]) -> Alert:
    event = payload.get("event")
    if not isinstance(event, Mapping):
        raise ValueError("PagerDuty webhook is missing event")
    data = event.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("PagerDuty webhook event is missing data")
    service_value = data.get("service")
    service_data = service_value if isinstance(service_value, Mapping) else {}
    event_id = _text(event.get("id"))
    incident_key = _text(data.get("id"))
    service = _text(service_data.get("summary")) or _text(service_data.get("id"))
    if not event_id or not incident_key or not service:
        raise ValueError("PagerDuty webhook is missing event, incident, or service identity")
    priority_value = data.get("priority")
    priority = priority_value if isinstance(priority_value, Mapping) else {}
    urgency = _text(data.get("urgency")).lower()
    return Alert(
        id=f"pagerduty:{event_id}",
        source="pagerduty",
        provider_event_id=event_id,
        provider_incident_key=incident_key,
        correlation_key=incident_key,
        title=_text(data.get("title"), "PagerDuty incident"),
        description=_text(data.get("description")),
        service=service,
        severity=_severity(priority.get("summary"), urgency=urgency),
        triggered_at=_timestamp(event.get("occurred_at")),
        tags={"event_type": _text(event.get("event_type"))},
        raw=dict(payload),
    )
