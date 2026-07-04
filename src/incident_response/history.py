"""Historical post-mortem retrieval for triage.

Each post-mortem markdown becomes a small, keyword-indexed record. When a new
alert arrives, we score every record by:
  1) exact service match (heavy weight)
  2) shared keywords between the alert title/description and the post-mortem body
  3) recency decay (a 6-month-old incident is worth less than yesterday's)

The top-K records are fed into the triage prompt as few-shot examples. This is a
deliberately simple retriever — no embedding model, no external index, no extra
dependencies. Good enough for hundreds of post-mortems; swap for a real vector
store once you have thousands.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import PriorIncident

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_STOPWORDS = frozenset(
    """
    the a an and or but of for in on at to from with by is are was were be been being
    this that these those it its as if then than so we our you your they their them
    have has had do does did will would could should may might can shall must not no
    one two three four five six seven eight nine ten first second third all any some
    each every none only more less most least new old same different
    incident postmortem post mortem summary impact timeline root cause detection
    mitigation lesson lessons learned action items severity duration
    """.split()
)


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS}


def _extract_service(content: str, filename: str) -> str:
    """Best-effort service extraction from post-mortem body or filename."""

    match = re.search(r"\*\*Service:\*\*\s*[`']?([A-Za-z0-9_.\-]+)", content, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    match = re.search(r"^Service:\s*([A-Za-z0-9_.\-]+)", content, re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group(1).lower()
    # Fall back: parse the incident id embedded in the filename "YYYY-MM-DD-inc-{svc}-{n}.md"
    stem = Path(filename).stem
    parts = stem.split("-")
    if "inc" in parts:
        idx = parts.index("inc")
        if idx + 1 < len(parts):
            return parts[idx + 1].lower()
    return ""


def _extract_root_cause(content: str) -> str:
    """Pull the Root Cause section if present, else the Summary."""

    for header in ("Root Cause", "Root cause", "root cause"):
        m = re.search(rf"##\s*{header}\s*\n(.+?)(?=\n##\s|\Z)", content, re.DOTALL)
        if m:
            return m.group(1).strip()[:500]
    m = re.search(r"##\s*Summary\s*\n(.+?)(?=\n##\s|\Z)", content, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()[:500]
    return content.strip()[:500]


def _extract_title(content: str, filename: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return Path(filename).stem


@dataclass(frozen=True)
class HistoricalIncident:
    path: str
    filename: str
    service: str
    title: str
    root_cause: str
    tokens: frozenset[str]
    written_at: datetime


@dataclass(frozen=True)
class HistoryMatch:
    incident: HistoricalIncident
    score: float


@dataclass
class PostmortemHistory:
    """Loads and searches a directory of markdown post-mortems."""

    directory: Path
    incidents: list[HistoricalIncident] = field(default_factory=list)

    @classmethod
    def load(cls, directory: Path) -> "PostmortemHistory":
        history = cls(directory=directory)
        history.refresh()
        return history

    def refresh(self) -> None:
        self.incidents = []
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("postmortem_read_failed", extra={"path": str(path), "error": str(exc)})
                continue
            written_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            self.incidents.append(
                HistoricalIncident(
                    path=str(path),
                    filename=path.name,
                    service=_extract_service(text, path.name),
                    title=_extract_title(text, path.name),
                    root_cause=_extract_root_cause(text),
                    tokens=frozenset(_tokenize(text)),
                    written_at=written_at,
                )
            )

    def search(
        self,
        *,
        service: str,
        query: str,
        now: datetime | None = None,
        top_k: int = 3,
        min_score: float = 0.15,
    ) -> list[HistoryMatch]:
        if not self.incidents:
            return []
        now = now or datetime.now(timezone.utc)
        query_tokens = _tokenize(query)
        service_key = service.lower()

        matches: list[HistoryMatch] = []
        for incident in self.incidents:
            score = self._score(incident, service_key, query_tokens, now)
            if score >= min_score:
                matches.append(HistoryMatch(incident=incident, score=score))

        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:top_k]

    @staticmethod
    def _score(
        incident: HistoricalIncident,
        service: str,
        query_tokens: set[str],
        now: datetime,
    ) -> float:
        # Service match dominates. Same service is a huge signal for triage similarity.
        service_score = 1.0 if incident.service and incident.service == service else 0.0

        # Keyword overlap — Jaccard, but weight token uniqueness a bit.
        if query_tokens and incident.tokens:
            overlap = query_tokens & incident.tokens
            union = query_tokens | incident.tokens
            keyword_score = len(overlap) / len(union) if union else 0.0
        else:
            keyword_score = 0.0

        # Recency decay: half-life of 180 days.
        age_days = max(0.0, (now - incident.written_at).total_seconds() / 86400)
        recency = math.exp(-math.log(2) * age_days / 180)

        return (0.55 * service_score) + (0.35 * keyword_score) + (0.10 * recency)


def to_prior_incident(match: HistoryMatch) -> PriorIncident:
    """Convert a retrieval hit into the display-facing PriorIncident model."""

    inc = match.incident
    return PriorIncident(
        title=inc.title,
        service=inc.service,
        date=inc.written_at.strftime("%Y-%m-%d"),
        root_cause=inc.root_cause,
        score=match.score,
        postmortem_path=inc.path,
    )


def format_for_prompt(matches: list[HistoryMatch]) -> str:
    """Render matches as compact few-shot context for the triage prompt."""

    if not matches:
        return ""
    lines = ["Prior similar incidents (most similar first):"]
    for m in matches:
        inc = m.incident
        when = inc.written_at.strftime("%Y-%m-%d")
        rc = " ".join(inc.root_cause.split())[:280]
        lines.append(
            f"- [{when}] service={inc.service or '?'} title=\"{inc.title}\" "
            f"score={m.score:.2f}\n  root_cause: {rc}"
        )
    return "\n".join(lines)
