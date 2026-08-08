"""Domain models. All values are immutable — use `model_copy(update=...)` to derive new ones."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Severity(str, Enum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"


class RemediationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"


class Alert(BaseModel):
    """Inbound alert payload from a monitoring system."""

    model_config = {"frozen": True}

    id: str
    source: str = "generic"
    provider_event_id: str = ""
    provider_incident_key: str | None = None
    correlation_key: str = ""
    title: str
    description: str = ""
    service: str
    severity: Severity = Severity.SEV3
    triggered_at: datetime
    metric: str | None = None
    environment: str = ""
    threshold: float | None = None
    value: float | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class Commit(BaseModel):
    model_config = {"frozen": True}

    sha: str
    author: str
    message: str
    timestamp: datetime
    files_changed: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    pr_number: int | None = None
    pr_url: str | None = None


class SuspectCommit(BaseModel):
    model_config = {"frozen": True}

    commit: Commit
    confidence: float  # 0..1
    reasoning: str


class Runbook(BaseModel):
    model_config = {"frozen": True}

    slug: str
    title: str
    tags: list[str]
    content: str
    path: str


class RunbookMatch(BaseModel):
    model_config = {"frozen": True}

    runbook: Runbook
    confidence: float
    reasoning: str


class ImpactEstimate(BaseModel):
    model_config = {"frozen": True}

    affected_users: int
    affected_percent: float
    error_rate: float
    reasoning: str
    time_window_minutes: int = 15


class PriorIncident(BaseModel):
    """Compact reference to a resolved past incident, surfaced in the Slack brief
    so on-call engineers see 'we've seen this before' with the resolution path."""

    model_config = {"frozen": True}

    title: str
    service: str
    date: str  # pre-formatted ISO date (YYYY-MM-DD) for display
    root_cause: str
    score: float
    postmortem_path: str


class TriageReport(BaseModel):
    """Aggregate output of the parallel triage phase."""

    model_config = {"frozen": True}

    suspects: list[SuspectCommit]
    runbook: RunbookMatch | None
    impact: ImpactEstimate
    summary: str
    prior_incidents: list[PriorIncident] = Field(default_factory=list)


class VerificationOutcome(BaseModel):
    """Persisted result of the post-remediation verification loop. Written back
    to the incident so future retrieval can prefer past matches whose runbook
    actually recovered the system."""

    model_config = {"frozen": True}

    status: str  # "recovered" | "improving" | "still_elevated" | "no_baseline"
    baseline_peak: float
    final_peak: float
    minutes_elapsed: float
    message: str
    runbook_slug: str | None = None


class RemediationStep(BaseModel):
    """Persisted preview of a proposed runbook action."""

    model_config = {"frozen": True}

    name: str
    command: str
    auto: bool = False


class RemediationRequest(BaseModel):
    """Approval and execution state for one incident's proposed remediation."""

    status: RemediationStatus
    runbook_slug: str
    requested_at: datetime
    steps: list[RemediationStep]
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str = ""
    execution_summary: str | None = None


class Incident(BaseModel):
    id: str
    alert: Alert
    related_alerts: list[Alert] = Field(default_factory=list)
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: datetime
    resolved_at: datetime | None = None
    triage: TriageReport | None = None
    slack_message_ts: str | None = None
    postmortem_path: str | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    verification_outcome: VerificationOutcome | None = None
    remediation: RemediationRequest | None = None
    on_call: list[OnCallResponder] = Field(default_factory=list)
    external_references: list[ExternalReference] = Field(default_factory=list)


@dataclass(frozen=True)
class CorrelationResult:
    incident: Incident
    created: bool
    duplicate: bool


class OnCallResponder(BaseModel):
    model_config = {"frozen": True}

    provider: str
    user_id: str
    name: str
    email: str = ""
    schedule: str = ""
    escalation_level: int = 1


class ExternalReference(BaseModel):
    model_config = {"frozen": True}

    provider: str
    external_id: str
    url: str


class OutboxMessage(BaseModel):
    model_config = {"frozen": True}

    id: str
    aggregate_id: str
    destination: str
    idempotency_key: str
    status: str = "pending"
    attempt_count: int = 0
    next_attempt_at: float = 0
    lease_token: str | None = None
    lease_expires_at: float | None = None


class MetricPoint(BaseModel):
    model_config = {"frozen": True}

    timestamp: datetime
    value: float


class MetricSeries(BaseModel):
    model_config = {"frozen": True}

    name: str
    points: list[MetricPoint]
    unit: str = ""
