"""FastAPI entrypoint.

Endpoints:
  POST /alerts                     → enqueue an incident (returns 202 + incident_id)
  POST /alerts/{id}/resolve        → mark resolved, generate post-mortem
  GET  /incidents/{id}             → fetch current state
  GET  /dead-letters               → list exhausted alerts
  POST /dead-letters/{id}/replay   → requeue an exhausted alert
  GET  /healthz
  GET  /readyz                     → reports queue depth and readiness
"""

from __future__ import annotations

import logging
import hmac
import json
import secrets
from inspect import isawaitable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse, StreamingResponse

from .agents.llm import AnthropicLLM, DemoLLM, LLM
from .auth import (
    AuthContext,
    AuthPolicy,
    Principal,
    Role,
    build_oidc_client,
    csv_groups,
    parse_bearer_token_roles,
)
from .config import Settings, load_settings
from .console import STATIC_DIR, register_console
from .db import IncidentStore
from .dedup import DedupIndex, RedisDedupIndex
from .executor import MockExecutor, RemediationExecutor, ShellExecutor
from .events import IncidentEventBroker, incident_version
from .integrations.github import build_github_client
from .integrations.metrics import build_metrics_client
from .integrations.on_call import OnCallClient, build_on_call_client
from .integrations.slack import build_slack_client
from .integrations.tickets import JiraTicketClient, LinearTicketClient, MockTicketClient
from .logging_config import configure_logging, set_incident_id, set_trace_id
from .models import Alert, Incident, IncidentStatus, Severity
from .normalization import normalize_datadog_alert, normalize_pagerduty_alert
from .orchestrator import IncidentOrchestrator, OrchestratorConfig
from .outbox import OutboxDispatcher, TicketClient
from .postgres import PostgresAlertStore, PostgresDatabase, PostgresIncidentStore
from .queue import (
    AlertAlreadyQueuedError,
    AlertQueue,
    DeadLetter,
    DeadLetterNotFoundError,
)
from .rate_limit import RedisSlidingWindowRateLimiter, SlidingWindowRateLimiter
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


def build_orchestrator(
    settings: Settings,
    llm: LLM | None = None,
    *,
    dedup: DedupIndex | RedisDedupIndex | None = None,
    store: IncidentStore | PostgresIncidentStore | None = None,
    on_call: OnCallClient | None = None,
    notifier: Any | None = None,
) -> IncidentOrchestrator:
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
    store = store or IncidentStore(settings.db_path)
    dedup = dedup or DedupIndex(ttl_seconds=settings.dedup_ttl_seconds)
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
        on_call=on_call,
        notifier=notifier,
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


class RemediationDecisionPayload(BaseModel):
    decided_by: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


def create_app(
    settings: Settings | None = None,
    llm: LLM | None = None,
    *,
    redis_client: Any | None = None,
    postgres_database: Any | None = None,
    oidc_client: Any | None = None,
    auth_policy: AuthPolicy | None = None,
    provider_http_client: httpx.AsyncClient | None = None,
    jira_http_client: httpx.AsyncClient | None = None,
    linear_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    setup_tracing(settings.otel_service_name)

    auth_policy = auth_policy or AuthPolicy(
        viewer_groups=csv_groups(settings.oidc_viewer_groups),
        responder_groups=csv_groups(settings.oidc_responder_groups),
        admin_groups=csv_groups(settings.oidc_admin_groups),
        groups_claim=settings.oidc_groups_claim,
        session_seconds=settings.session_seconds,
    )
    auth = AuthContext(
        enabled=settings.auth_mode == "oidc",
        policy=auth_policy,
        bearer_token_roles=parse_bearer_token_roles(settings.operator_bearer_tokens),
    )
    if auth.enabled and oidc_client is None:
        oidc_client = build_oidc_client(settings)

    owns_redis = False
    if settings.redis_url:
        if redis_client is None:
            try:
                import redis.asyncio as redis
            except ImportError as exc:
                raise RuntimeError(
                    "REDIS_URL requires the production dependencies; "
                    "install incident-response[production]"
                ) from exc
            redis_client = redis.from_url(settings.redis_url, decode_responses=True)
            owns_redis = True
        dedup = RedisDedupIndex(
            redis_client,
            ttl_seconds=settings.dedup_ttl_seconds,
            namespace=settings.redis_namespace,
        )
        limiter = RedisSlidingWindowRateLimiter(
            redis_client,
            max_events=settings.rate_limit_max,
            window_seconds=settings.rate_limit_window_seconds,
            namespace=settings.redis_namespace,
        )
    else:
        dedup = DedupIndex(ttl_seconds=settings.dedup_ttl_seconds)
        limiter = SlidingWindowRateLimiter(
            max_events=settings.rate_limit_max,
            window_seconds=settings.rate_limit_window_seconds,
        )

    ticket_clients: dict[str, TicketClient] = {}
    owned_ticket_http: list[httpx.AsyncClient] = []
    if settings.jira_mode == "mock":
        ticket_clients["jira"] = MockTicketClient("jira")
    elif settings.jira_mode == "jira":
        if jira_http_client is None:
            jira_http_client = httpx.AsyncClient(
                base_url=settings.jira_base_url.rstrip("/"),
                timeout=10,
            )
            owned_ticket_http.append(jira_http_client)
        ticket_clients["jira"] = JiraTicketClient(
            base_url=settings.jira_base_url,
            email=settings.jira_email,
            api_token=settings.jira_api_token,
            project_key=settings.jira_project_key,
            issue_type=settings.jira_issue_type,
            http=jira_http_client,
        )
    if settings.linear_mode == "mock":
        ticket_clients["linear"] = MockTicketClient("linear")
    elif settings.linear_mode == "linear":
        if linear_http_client is None:
            linear_http_client = httpx.AsyncClient(
                base_url="https://api.linear.app",
                timeout=10,
            )
            owned_ticket_http.append(linear_http_client)
        ticket_clients["linear"] = LinearTicketClient(
            token=settings.linear_api_token,
            team_id=settings.linear_team_id,
            http=linear_http_client,
        )
    outbox_destinations = tuple(ticket_clients)

    if settings.database_url:
        postgres_database = postgres_database or PostgresDatabase(settings.database_url)
        incident_store = PostgresIncidentStore(
            postgres_database,
            outbox_destinations=outbox_destinations,
        )
        queue_store = PostgresAlertStore(postgres_database)
        queue_db_path = None
    else:
        incident_store = IncidentStore(
            settings.db_path,
            outbox_destinations=outbox_destinations,
        )
        queue_store = None
        queue_db_path = settings.db_path

    owns_provider_http = False
    if settings.pagerduty_mode == "pagerduty" and provider_http_client is None:
        provider_http_client = httpx.AsyncClient(
            base_url="https://api.pagerduty.com",
            timeout=10,
        )
        owns_provider_http = True
    on_call = build_on_call_client(
        mode=settings.pagerduty_mode,
        token=settings.pagerduty_api_token,
        service_ids=settings.pagerduty_service_ids,
        http=provider_http_client,
    )
    event_broker = IncidentEventBroker(
        redis_client,
        namespace=settings.redis_namespace,
    )
    orchestrator = build_orchestrator(
        settings,
        llm=llm,
        dedup=dedup,
        store=incident_store,
        on_call=on_call,
        notifier=event_broker.publish,
    )
    queue = AlertQueue(
        handler=orchestrator.handle_alert,
        db_path=queue_db_path,
        store=queue_store,
        retry_base_seconds=settings.queue_retry_base_seconds,
        retry_max_seconds=settings.queue_retry_max_seconds,
        max_attempts=settings.queue_max_attempts,
        lease_seconds=settings.queue_lease_seconds,
    )
    outbox = OutboxDispatcher(
        store=incident_store,
        ticket_clients=ticket_clients,
        max_attempts=settings.outbox_max_attempts,
        retry_base_seconds=settings.outbox_retry_base_seconds,
        retry_max_seconds=settings.outbox_retry_max_seconds,
        lease_seconds=settings.outbox_lease_seconds,
        poll_seconds=settings.outbox_poll_seconds,
    )
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if postgres_database is not None:
            postgres_database.open()
            postgres_database.migrate()
        queue.start()
        outbox.start()
        logger.info("app_started", extra={"worker": "alert-queue"})
        try:
            yield
        finally:
            await queue.stop()
            await outbox.stop()
            if owns_redis:
                await redis_client.aclose()
            if owns_provider_http and provider_http_client is not None:
                await provider_http_client.aclose()
            for client in owned_ticket_http:
                await client.aclose()
            if postgres_database is not None:
                postgres_database.close()
            logger.info("app_stopped")

    app = FastAPI(title="Autonomous Incident Response", version="0.2.0", lifespan=lifespan)
    if auth.enabled:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret or secrets.token_urlsafe(32),
            session_cookie="incident_response_session",
            max_age=settings.session_seconds,
            same_site="lax",
            https_only=settings.session_https_only,
        )
    instrument_app(app)

    app.state.orchestrator = orchestrator
    app.state.queue = queue
    app.state.outbox = outbox
    app.state.event_broker = event_broker
    app.state.limiter = limiter
    app.state.redis = redis_client
    app.state.postgres_database = postgres_database
    app.state.auth = auth
    app.state.settings = settings

    @app.middleware("http")
    async def browser_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
            "style-src 'self'; img-src 'self'; connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        )
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )
        return response

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
        token_ok = bool(settings.webhook_token) and hmac.compare_digest(
            token,
            settings.webhook_token,
        )

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

    async def _verify_provider(request: Request, provider: str) -> bytes:
        body = await request.body()
        token = request.headers.get("x-webhook-token", "")
        token_ok = bool(settings.webhook_token) and hmac.compare_digest(
            token,
            settings.webhook_token,
        )
        if provider == "datadog":
            signature_ok = verify_datadog(
                settings.datadog_webhook_secret,
                body,
                request.headers.get("x-datadog-signature", ""),
            )
        else:
            signature_ok = verify_pagerduty(
                settings.pagerduty_webhook_secret,
                body,
                request.headers.get("x-pagerduty-signature", ""),
            )
        if not (token_ok or signature_ok):
            raise HTTPException(status_code=401, detail="Invalid webhook credentials")
        return body

    async def _require_operator(
        request: Request,
        role: Role,
        *,
        csrf: bool = False,
        legacy_webhook: bool = False,
        redirect_to_login: bool = False,
    ) -> Principal:
        if auth.enabled:
            return await auth.require(
                request,
                role,
                csrf=csrf,
                redirect_to_login=redirect_to_login,
            )
        if legacy_webhook:
            await _verify_inbound(request)
        principal = auth.principal(request)
        assert principal is not None
        return principal

    async def _viewer(request: Request) -> Principal:
        return await _require_operator(request, Role.VIEWER)

    async def _legacy_viewer(request: Request) -> Principal:
        return await _require_operator(request, Role.VIEWER, legacy_webhook=True)

    async def _responder_write(request: Request) -> Principal:
        return await _require_operator(
            request,
            Role.RESPONDER,
            csrf=True,
            legacy_webhook=True,
        )

    async def _viewer_write(request: Request) -> Principal:
        return await _require_operator(
            request,
            Role.VIEWER,
            csrf=True,
        )

    async def _admin_write(request: Request) -> Principal:
        return await _require_operator(
            request,
            Role.ADMIN,
            csrf=True,
            legacy_webhook=True,
        )

    async def _console_viewer(request: Request) -> Principal:
        return await _require_operator(
            request,
            Role.VIEWER,
            redirect_to_login=True,
        )

    async def _console_responder_write(request: Request) -> Principal:
        return await _require_operator(
            request,
            Role.RESPONDER,
            csrf=True,
            redirect_to_login=True,
        )

    async def _console_admin_write(request: Request) -> Principal:
        return await _require_operator(
            request,
            Role.ADMIN,
            csrf=True,
            redirect_to_login=True,
        )

    if auth.enabled:

        @app.get("/auth/login")
        async def login(request: Request):
            redirect_uri = request.url_for("auth_callback")
            return await oidc_client.authorize_redirect(request, redirect_uri)

        @app.get("/auth/callback", name="auth_callback")
        async def auth_callback(request: Request):
            token = await oidc_client.authorize_access_token(request)
            try:
                session = auth_policy.session_from_oidc_token(token)
            except PermissionError as exc:
                raise HTTPException(
                    status_code=403,
                    detail="OIDC user is not authorized",
                ) from exc
            request.session.clear()
            request.session.update(session)
            return RedirectResponse("/console", status_code=303)

        @app.post("/auth/logout")
        async def logout(
            request: Request,
            _principal: Principal = Depends(_viewer_write),
        ):
            request.session.clear()
            return RedirectResponse("/console", status_code=303)

    async def _rate_limit(request: Request, service: str) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"{client_ip}|{service}"
        allowed = limiter.check(key)
        if isawaitable(allowed):
            allowed = await allowed
        if not allowed:
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

    @app.get("/dead-letters")
    async def list_dead_letters(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        _principal: Principal = Depends(_legacy_viewer),
    ) -> list[DeadLetter]:
        return queue.list_dead_letters(limit=limit)

    @app.post("/dead-letters/{alert_id}/replay", status_code=202)
    async def replay_dead_letter(
        alert_id: str,
        request: Request,
        _principal: Principal = Depends(_admin_write),
    ) -> dict[str, str]:
        try:
            alert = await queue.replay_dead_letter(
                alert_id,
                before_wake=orchestrator.prepare_replay,
            )
        except DeadLetterNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Dead letter not found") from exc
        except AlertAlreadyQueuedError as exc:
            raise HTTPException(status_code=409, detail="Alert is already queued") from exc
        incident_id = f"inc-{alert.id}"
        set_incident_id(incident_id)
        logger.info("dead_letter_replayed", extra={"alert_id": alert.id})
        return {"status": "replayed", "incident_id": incident_id}

    @app.post("/alerts", status_code=202)
    async def fire_alert(
        payload: AlertPayload,
        request: Request,
        _body: bytes = Depends(_verify_inbound),
    ) -> dict[str, str]:
        await _rate_limit(request, payload.service)
        alert = payload.to_alert()
        incident_id = f"inc-{alert.id}"
        set_incident_id(incident_id)
        await queue.submit(alert)
        logger.info("alert_enqueued", extra={"alert_id": alert.id, "service": alert.service})
        return {"status": "accepted", "incident_id": incident_id}

    async def _accept_provider_alert(request: Request, provider: str) -> dict[str, str]:
        await _verify_provider(request, provider)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            alert = (
                normalize_datadog_alert(payload)
                if provider == "datadog"
                else normalize_pagerduty_alert(payload)
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {provider} alert payload",
            ) from exc
        await _rate_limit(request, alert.service)
        await queue.submit(alert)
        incident_id = f"inc-{alert.id}"
        set_incident_id(incident_id)
        logger.info(
            "alert_enqueued",
            extra={
                "alert_id": alert.id,
                "service": alert.service,
                "provider": provider,
            },
        )
        return {"status": "accepted", "incident_id": incident_id}

    @app.post("/alerts/datadog", status_code=202)
    async def fire_datadog_alert(request: Request) -> dict[str, str]:
        return await _accept_provider_alert(request, "datadog")

    @app.post("/alerts/pagerduty", status_code=202)
    async def fire_pagerduty_alert(request: Request) -> dict[str, str]:
        return await _accept_provider_alert(request, "pagerduty")

    @app.post("/alerts/{incident_id}/resolve")
    async def resolve(
        incident_id: str,
        payload: ResolvePayload,
        request: Request,
        _principal: Principal = Depends(_responder_write),
    ) -> Incident:
        set_incident_id(incident_id)
        try:
            return await orchestrator.resolve(incident_id, payload.resolution_note)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/alerts/{incident_id}/remediation/approve")
    async def approve_remediation(
        incident_id: str,
        payload: RemediationDecisionPayload,
        request: Request,
        principal: Principal = Depends(_admin_write),
    ) -> Incident:
        set_incident_id(incident_id)
        try:
            return await orchestrator.approve_remediation(
                incident_id,
                decided_by=(
                    principal.email or principal.subject
                    if auth.enabled
                    else payload.decided_by
                ),
                note=payload.note,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/alerts/{incident_id}/remediation/reject")
    async def reject_remediation(
        incident_id: str,
        payload: RemediationDecisionPayload,
        request: Request,
        principal: Principal = Depends(_responder_write),
    ) -> Incident:
        set_incident_id(incident_id)
        try:
            return await orchestrator.reject_remediation(
                incident_id,
                decided_by=(
                    principal.email or principal.subject
                    if auth.enabled
                    else payload.decided_by
                ),
                note=payload.note,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/incidents")
    async def list_incidents(
        _principal: Principal = Depends(_viewer),
        incident_status: IncidentStatus | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[Incident]:
        return orchestrator.store.list_recent(limit=limit, status=incident_status)

    @app.get("/incidents/{incident_id}")
    async def get_incident(
        incident_id: str,
        _principal: Principal = Depends(_viewer),
    ) -> Incident:
        set_incident_id(incident_id)
        incident = orchestrator.store.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="not found")
        return incident

    @app.get("/events/incidents/{incident_id}")
    async def incident_events(
        incident_id: str,
        request: Request,
        _principal: Principal = Depends(_viewer),
    ) -> StreamingResponse:
        if orchestrator.store.get(incident_id) is None:
            raise HTTPException(status_code=404, detail="not found")

        async def stream():
            last_version = request.headers.get("last-event-id", "")
            incident = orchestrator.store.get(incident_id)
            if incident is not None:
                version = incident_version(incident)
                last_version = version
                yield (
                    f"id: {version}\n"
                    "event: incident\n"
                    f"data: {json.dumps({'incident_id': incident_id, 'version': version})}\n\n"
                )
            async for changed in event_broker.events(incident_id):
                if await request.is_disconnected():
                    break
                if not changed:
                    yield ": heartbeat\n\n"
                    continue
                incident = orchestrator.store.get(incident_id)
                if incident is None:
                    continue
                version = incident_version(incident)
                if version == last_version:
                    continue
                last_version = version
                yield (
                    f"id: {version}\n"
                    "event: incident\n"
                    f"data: {json.dumps({'incident_id': incident_id, 'version': version})}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    register_console(
        app,
        orchestrator=orchestrator,
        queue=queue,
        settings=settings,
        auth=auth,
        viewer_dependency=_console_viewer,
        responder_write_dependency=_console_responder_write,
        admin_write_dependency=_console_admin_write,
    )

    return app


def run() -> None:
    from .cli import main

    raise SystemExit(main())


if __name__ == "__main__":
    run()
