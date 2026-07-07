"""FastAPI entrypoint.

Endpoints:
  POST /alerts                     → enqueue an incident (returns 202 + incident_id)
  POST /alerts/{id}/resolve        → mark resolved, generate post-mortem
  GET  /incidents/{id}             → fetch current state
  GET  /healthz
  GET  /readyz                     → reports queue depth and readiness
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from .agents.llm import AnthropicLLM, DemoLLM, LLM
from .config import Settings, load_settings
from .db import IncidentStore
from .dedup import DedupIndex
from .executor import MockExecutor, RemediationExecutor, ShellExecutor
from .integrations.github import build_github_client
from .integrations.metrics import build_metrics_client
from .integrations.slack import build_slack_client
from .logging_config import configure_logging, set_incident_id, set_trace_id
from .models import Alert, Incident, Severity
from .orchestrator import IncidentOrchestrator, OrchestratorConfig
from .queue import AlertQueue
from .rate_limit import SlidingWindowRateLimiter
from .security import verify_datadog, verify_generic_hmac, verify_pagerduty
from .telemetry import current_trace_id, instrument_app, setup_tracing

logger = logging.getLogger(__name__)


def build_executor(settings: Settings) -> RemediationExecutor:
    if settings.remediation_mode == "shell":
        allowed = frozenset(
            s.strip() for s in settings.remediation_allowed_commands.split(",") if s.strip()
        )
        return ShellExecutor(
            allowed_prefixes=allowed, timeout_seconds=settings.remediation_timeout_seconds
        )
    return MockExecutor()


def build_orchestrator(settings: Settings, llm: LLM | None = None) -> IncidentOrchestrator:
    if llm is None:
        if settings.llm_mode == "mock":
            llm = DemoLLM()
        elif settings.llm_mode != "anthropic":
            raise RuntimeError(
                f"Unsupported LLM_MODE={settings.llm_mode!r}. Use 'anthropic' or 'mock'."
            )
    if llm is None:
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Set it in .env, set LLM_MODE=mock "
                "for an offline demo, or inject an LLM for tests."
            )
        llm = AnthropicLLM(api_key=settings.anthropic_api_key, model=settings.anthropic_model)

    github = build_github_client(settings.github_mode, settings.github_token, settings.github_repo)
    slack = build_slack_client(
        settings.slack_mode, settings.slack_webhook_url, settings.slack_bot_token
    )
    metrics = build_metrics_client(
        settings.metrics_mode, settings.datadog_api_key, settings.datadog_app_key
    )
    store = IncidentStore(settings.db_path)
    dedup = DedupIndex(ttl_seconds=settings.dedup_ttl_seconds)
    executor = build_executor(settings)

    config = OrchestratorConfig(
        slack_channel=settings.slack_channel,
        runbooks_dir=settings.runbooks_dir,
        postmortem_dir=settings.postmortem_dir,
        dedup_bucket_minutes=settings.dedup_bucket_minutes,
        verification_enabled=settings.verification_enabled,
        verification_total_minutes=settings.verification_total_minutes,
        verification_poll_seconds=settings.verification_poll_seconds,
    )
    return IncidentOrchestrator(
        llm=llm,
        github=github,
        slack=slack,
        metrics=metrics,
        store=store,
        config=config,
        dedup=dedup,
        executor=executor,
    )


class AlertPayload(BaseModel):
    id: str
    title: str
    description: str = ""
    service: str
    severity: Severity = Severity.SEV3
    triggered_at: datetime
    metric: str | None = None
    threshold: float | None = None
    value: float | None = None
    tags: dict[str, str] = {}
    raw: dict[str, Any] = {}

    def to_alert(self) -> Alert:
        return Alert(**self.model_dump())


class ResolvePayload(BaseModel):
    resolution_note: str = ""


def create_app(settings: Settings | None = None, llm: LLM | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    setup_tracing(settings.otel_service_name)

    orchestrator = build_orchestrator(settings, llm=llm)
    queue = AlertQueue(handler=orchestrator.handle_alert)
    limiter = SlidingWindowRateLimiter(
        max_events=settings.rate_limit_max,
        window_seconds=settings.rate_limit_window_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        queue.start()
        logger.info("app_started", extra={"worker": "alert-queue"})
        try:
            yield
        finally:
            await queue.stop()
            logger.info("app_stopped")

    app = FastAPI(title="Autonomous Incident Response", version="0.2.0", lifespan=lifespan)
    instrument_app(app)

    app.state.orchestrator = orchestrator
    app.state.queue = queue
    app.state.limiter = limiter
    app.state.settings = settings

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        set_trace_id(current_trace_id())
        set_incident_id(None)
        try:
            return await call_next(request)
        finally:
            set_trace_id(None)
            set_incident_id(None)

    async def _verify_inbound(request: Request) -> bytes:
        body = await request.body()
        token = request.headers.get("x-webhook-token", "")
        token_ok = bool(settings.webhook_token) and token == settings.webhook_token

        dd_sig = request.headers.get("x-datadog-signature", "")
        pd_sig = request.headers.get("x-pagerduty-signature", "")
        generic_sig = request.headers.get("x-webhook-signature", "")

        hmac_ok = (
            (settings.datadog_webhook_secret and verify_datadog(settings.datadog_webhook_secret, body, dd_sig))
            or (settings.pagerduty_webhook_secret and verify_pagerduty(settings.pagerduty_webhook_secret, body, pd_sig))
            or (settings.generic_webhook_secret and verify_generic_hmac(settings.generic_webhook_secret, body, generic_sig))
        )

        # Any one valid credential is enough. Token is the default; HMAC is stronger if set.
        if not (token_ok or hmac_ok):
            raise HTTPException(status_code=401, detail="Invalid webhook credentials")
        return body

    def _rate_limit(request: Request, service: str) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"{client_ip}|{service}"
        if not limiter.check(key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"rate limited for {key}",
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        return {"status": "ok", "queue_depth": queue.qsize()}

    @app.post("/alerts", status_code=202)
    async def fire_alert(
        payload: AlertPayload,
        request: Request,
        _body: bytes = Depends(_verify_inbound),
    ) -> dict[str, str]:
        _rate_limit(request, payload.service)
        alert = payload.to_alert()
        incident_id = f"inc-{alert.id}"
        set_incident_id(incident_id)
        await queue.submit(alert)
        logger.info("alert_enqueued", extra={"alert_id": alert.id, "service": alert.service})
        return {"status": "accepted", "incident_id": incident_id}

    @app.post("/alerts/{incident_id}/resolve")
    async def resolve(
        incident_id: str,
        payload: ResolvePayload,
        request: Request,
        _body: bytes = Depends(_verify_inbound),
    ) -> Incident:
        set_incident_id(incident_id)
        try:
            return await orchestrator.resolve(incident_id, payload.resolution_note)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str) -> Incident:
        set_incident_id(incident_id)
        incident = orchestrator._store.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="not found")
        return incident

    return app


def run() -> None:
    from .cli import main

    raise SystemExit(main())


if __name__ == "__main__":
    run()
