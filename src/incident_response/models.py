"""Domain models. All values are immutable — use `model_copy(update=...)` to derive new ones."""

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


class Alert(BaseModel):
    """Inbound alert payload from a monitoring system."""

    model_config = {"frozen": True}

    id: str
    title: str
    description: str = ""
    service: str
    severity: Severity = Severity.SEV3
    triggered_at: datetime
    metric: str | None = None
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


class TriageReport(BaseModel):
    """Aggregate output of the parallel triage phase."""

    model_config = {"frozen": True}

    suspects: list[SuspectCommit]
    runbook: RunbookMatch | None
    impact: ImpactEstimate
    summary: str


class Incident(BaseModel):
    id: str
    alert: Alert
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: datetime
    resolved_at: datetime | None = None
    triage: TriageReport | None = None
    slack_message_ts: str | None = None
    postmortem_path: str | None = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class MetricPoint(BaseModel):
    model_config = {"frozen": True}

    timestamp: datetime
    value: float


class MetricSeries(BaseModel):
    model_config = {"frozen": True}

    name: str
    points: list[MetricPoint]
    unit: str = ""
