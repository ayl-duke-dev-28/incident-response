# Autonomous Incident Response

Autonomous Incident Response turns a production alert into an investigation brief,
candidate root cause, matched runbook, safe remediation summary, and post-mortem.
It is built as a local-first FastAPI service with deterministic mock adapters, so
you can run the full flow without external credentials before wiring it to
Anthropic, GitHub, Slack, and Datadog.

## What You Can Do

- Receive Datadog, PagerDuty, or generic webhook alerts at `POST /alerts`.
- Authenticate webhooks with a shared token or HMAC signatures.
- Return `202 Accepted` quickly and process the incident in a background worker.
- Deduplicate repeated alerts in a 15-minute service/metric/severity bucket.
- Triage recent commits, match the best runbook, and estimate user impact in parallel.
- Stream a Slack incident brief as each agent finishes.
- Annotate the suspect PR when confidence clears the configured floor.
- Dry-run or execute allow-listed runbook actions.
- Verify whether remediation actually reduced the error rate.
- Persist incident state to SQLite after every major step.
- List recent incidents without knowing an incident ID.
- Inspect open and recently resolved incidents, including full triage and timeline
  detail, in a local web console.
- Trigger a safe, collision-free demo incident from the console when every
  integration and remediation mode is mocked.
- Resolve triaged incidents from the all-mock console and generate their
  post-mortems without switching to the JSON API.
- Generate a blameless post-mortem when the incident is resolved.
- Run a complete offline demo with no Anthropic key and no external services.

## First Run: Offline Demo

The fastest way to see the product work is the built-in demo. It drives the real
FastAPI routes in-process, using mock GitHub, Slack, metrics, remediation, and
LLM adapters. Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

incident-response demo
```

Expected output:

```text
accepted inc-demo-checkout-001
triaged checkout-error-rate suspect=a1b2c3d
fetched inc-demo-checkout-001 status=investigating
resolved inc-demo-checkout-001
postmortem demo-postmortems/YYYY-MM-DD-inc-demo-checkout-001.md
```

That run covers the full golden path:

```text
alert -> triage -> runbook match -> impact estimate -> incident fetch -> resolve -> postmortem
```

## Run The API Locally

Use mock LLM mode for local API development. This keeps the first server run free
from Anthropic credentials while still exercising the real queue, orchestrator,
storage, runbook, Slack mock, and metrics mock paths.

```bash
cp .env.example .env
LLM_MODE=mock incident-response serve --host 127.0.0.1 --reload --port 8080
```

Open:

- API docs: `http://localhost:8080/docs`
- OpenAPI schema: `http://localhost:8080/openapi.json`
- Health: `http://localhost:8080/healthz`
- Readiness: `http://localhost:8080/readyz`

Send a test alert:

```bash
curl -X POST http://localhost:8080/alerts \
  -H "x-webhook-token: change-me" \
  -H "content-type: application/json" \
  -d '{
    "id": "ddg-9273",
    "title": "Checkout 5xx > 5%",
    "description": "checkout service error rate at 18%",
    "service": "checkout",
    "severity": "sev2",
    "triggered_at": "2026-07-02T21:05:00+00:00",
    "metric": "http.error_rate",
    "threshold": 0.05,
    "value": 0.184,
    "tags": {"env": "demo"}
  }'
```

Response:

```json
{"status":"accepted","incident_id":"inc-ddg-9273"}
```

Fetch the incident after triage finishes:

```bash
curl http://localhost:8080/incidents/inc-ddg-9273
```

List recent incidents:

```bash
curl http://localhost:8080/incidents
```

Filter or limit the list:

```bash
curl "http://localhost:8080/incidents?status=investigating&limit=10"
```

Resolve it and generate a post-mortem:

```bash
curl -X POST http://localhost:8080/alerts/inc-ddg-9273/resolve \
  -H "x-webhook-token: change-me" \
  -H "content-type: application/json" \
  -d '{"resolution_note": "rolled back a1b2c3d"}'
```

Post-mortems are written to `./postmortems/YYYY-MM-DD-inc-*.md`.

## Incident Console

The console is a server-rendered operator view over the same incident data the API
returns. It needs no frontend build, no template engine, and no external assets.

```bash
LLM_MODE=mock \
GITHUB_MODE=mock \
SLACK_MODE=mock \
METRICS_MODE=mock \
REMEDIATION_MODE=mock \
incident-response serve --host 127.0.0.1 --reload --port 8080
```

Open `http://localhost:8080/console`.

The incident list shows severity, service, title, status, age, metric value,
matched runbook, and top suspect confidence, with open incidents above recently
resolved ones. With no incidents stored, the page shows an empty state instead.

Select **Trigger demo incident** to enqueue a unique checkout scenario. The
console waits for the worker to persist it, then redirects to its detail page.
Triage continues in the background, so reload to see the suspect, runbook, and
impact sections fill in. You can also populate the console by sending the test
alert from the API section above.

Select an incident title to open `/console/incidents/{id}`. The detail page shows:

- alert description, service, metric, threshold, current value, and tags;
- triage summary, estimated impact, matched runbook, and suspect commits;
- the remediation and resolution timeline;
- verification results and the generated post-mortem path when available.

Incidents still being triaged render an in-progress state instead of incomplete
sections. Once triage finishes in all-mock mode, the detail page shows a
**Resolve incident** form. Submit an optional note of up to 500 characters to
mark the incident resolved, generate its post-mortem, and return to the updated
detail page. Unknown incident IDs return a navigable HTML `404` page.

The demo button is shown only when LLM, GitHub, Slack, metrics, and remediation
modes are all `mock`. It is hidden and `POST /console/demo-alert` returns `403` if
any mode could reach a real integration or execute shell remediation. Cross-site
browser submissions are also rejected.

The resolve form has the same all-mock and same-origin restrictions. It is hidden
until triage finishes and after the incident is resolved. In real integration
modes, resolve incidents through the authenticated `POST /alerts/{id}/resolve`
API instead.

What works today:

| Surface | Status |
|---|---|
| `GET /console` incident list | Working. |
| `GET /console/incidents/{id}` incident detail | Working. Shows alert, triage, impact, runbook, suspect, timeline, verification, and resolution data. |
| `GET /static/console.css` | Working. |
| `POST /console/demo-alert` demo action | Working in all-mock mode. Enqueues a unique demo incident and redirects to its detail page. Hidden and forbidden when any integration or remediation mode is not `mock`. |
| `POST /console/incidents/{id}/resolve` resolve action | Working in all-mock mode after triage completes. Accepts a form-encoded resolution note, generates a post-mortem, and redirects to the resolved detail page. |

The console is local-first and unauthenticated. See Current Limits before exposing
it on anything other than localhost.

## CLI

```bash
incident-response --help
incident-response demo --help
incident-response serve --help
```

Commands:

| Command | Purpose |
|---|---|
| `incident-response demo` | Run the full incident lifecycle offline. |
| `incident-response serve` | Start the FastAPI server. Defaults to `0.0.0.0:8080`; pass `--host 127.0.0.1` when using the unauthenticated console locally. |

Useful demo flags:

| Flag | Default | Purpose |
|---|---|---|
| `--db-path` | `./demo-incidents.db` | SQLite file for demo incident state. |
| `--postmortem-dir` | `./demo-postmortems` | Directory for generated post-mortems. |
| `--runbooks-dir` | `./runbooks` | Runbook library used by the demo. |
| `--webhook-token` | `demo-secret` | Token used by the in-process demo request. |
| `--timeout-seconds` | `5.0` | Max time to wait for async triage. |

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/alerts` | Enqueue an incident. Returns `202 {status, incident_id}`. |
| `GET` | `/incidents` | List recent incidents, newest first. Supports `status` and `limit` query params. |
| `GET` | `/incidents/{id}` | Fetch current incident state. |
| `POST` | `/alerts/{id}/resolve` | Mark resolved and generate a post-mortem. |
| `GET` | `/healthz` | Liveness check. |
| `GET` | `/readyz` | Liveness plus queue depth. |
| `GET` | `/console` | Operator incident list. Returns HTML, not JSON. Unauthenticated. |
| `GET` | `/console/incidents/{id}` | Operator incident detail. Returns HTML, including an HTML `404` for unknown IDs. Unauthenticated. |
| `POST` | `/console/demo-alert` | Enqueue a unique checkout demo and redirect to its detail page. Available only when all integrations and remediation use `mock`. |
| `POST` | `/console/incidents/{id}/resolve` | Resolve a triaged incident from the console and redirect to its detail page. Accepts form data with an optional `resolution_note` of at most 500 characters. Available only in all-mock mode. |
| `GET` | `/static/console.css` | Console stylesheet. |

Incident list query params:

| Param | Default | Notes |
|---|---:|---|
| `status` | none | Optional filter: `open`, `investigating`, `mitigated`, or `resolved`. Invalid values return `422`. |
| `limit` | `50` | Max recent incidents to return. Must be between `1` and `200`; out-of-range values return `422`. |

When both `status` and `limit` are provided, the API filters by status before
applying the limit, so older matching incidents are still returned.

`GET /incidents` returns an empty JSON array when no incidents exist. Each item is
the same incident object returned by `GET /incidents/{id}`:

```json
[
  {
    "id": "inc-ddg-9273",
    "status": "investigating",
    "created_at": "2026-07-02T21:05:00Z",
    "alert": {
      "id": "ddg-9273",
      "title": "Checkout 5xx > 5%",
      "service": "checkout",
      "severity": "sev2",
      "triggered_at": "2026-07-02T21:05:00Z"
    }
  }
]
```

Alert fields:

| Field | Required | Notes |
|---|---:|---|
| `id` | yes | Provider alert ID. Used in the generated incident ID. |
| `title` | yes | Human-readable alert title. |
| `service` | yes | Service name used for rate limit, dedup, metrics, and commit lookup. |
| `triggered_at` | yes | ISO 8601 timestamp. |
| `severity` | no | One of `sev1`, `sev2`, `sev3`, `sev4`. Defaults to `sev3`. |
| `description` | no | Extra context for triage and history retrieval. |
| `metric` | no | Metric name, for example `http.error_rate`. |
| `threshold` | no | Alert threshold. |
| `value` | no | Current metric value. |
| `tags` | no | Provider tags. |
| `raw` | no | Original provider payload. |

Authentication:

- Shared token: `X-Webhook-Token: <WEBHOOK_TOKEN>`
- Datadog HMAC: `X-Datadog-Signature`
- PagerDuty HMAC: `X-PagerDuty-Signature`
- Generic HMAC: `X-Webhook-Signature`

Any one valid credential is enough.

## Configuration

All runtime settings are loaded from environment variables or `.env`. The values
below are the defaults unless the row says a credential is required.

| Env var | Modes / example | Notes |
|---|---|---|
| `LLM_MODE` | `anthropic` | Supports `anthropic` or `mock`; `mock` uses deterministic local responses. |
| `ANTHROPIC_API_KEY` | empty | Required when `LLM_MODE=anthropic`. |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Anthropic model name. |
| `GITHUB_MODE` | `mock` | Supports `mock` or `rest`; `rest` needs `GITHUB_TOKEN` and `GITHUB_REPO`. |
| `GITHUB_TOKEN` | empty | GitHub bearer token used in `rest` mode. |
| `GITHUB_REPO` | `owner/repo` | Repository in `owner/repo` form. |
| `SLACK_MODE` | `mock` | Supports `mock`, `webhook`, or `bot`; `bot` enables in-place `chat.update` streaming. |
| `SLACK_WEBHOOK_URL` | empty | Required for webhook mode; progress is posted as threaded messages rather than in-place updates. |
| `SLACK_BOT_TOKEN` | empty | Required for bot mode and should have `chat:write`. |
| `SLACK_CHANNEL` | `#incidents` | Destination channel for incident messages. |
| `METRICS_MODE` | `mock` | Supports `mock` or `datadog`; Datadog mode needs both Datadog keys. |
| `DATADOG_API_KEY` | empty | Datadog API key. |
| `DATADOG_APP_KEY` | empty | Datadog application key. |
| `RUNBOOKS_DIR` | `./runbooks` | Markdown runbook library. |
| `POSTMORTEM_DIR` | `./postmortems` | Generated post-mortems. |
| `DB_PATH` | `./incidents.db` | SQLite incident store. |
| `WEBHOOK_TOKEN` | `change-me` | Shared webhook token. |
| `DATADOG_WEBHOOK_SECRET` | empty | Optional Datadog HMAC secret. |
| `PAGERDUTY_WEBHOOK_SECRET` | empty | Optional PagerDuty HMAC secret. |
| `GENERIC_WEBHOOK_SECRET` | empty | Optional generic HMAC-SHA256 secret. |
| `RATE_LIMIT_MAX` | `30` | Maximum alerts per client-IP/service window. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding rate-limit window in seconds. |
| `DEDUP_BUCKET_MINUTES` | `15` | Timestamp bucket used in alert fingerprints. |
| `DEDUP_TTL_SECONDS` | `3600` | In-memory fingerprint lifetime in seconds. |
| `REMEDIATION_MODE` | `mock` | Supports `mock` or `shell`; shell mode can run explicitly automated, allow-listed commands. |
| `REMEDIATION_ALLOWED_COMMANDS` | `feature-flag,kubectl,deploy` | First-token allow list for shell remediation. |
| `REMEDIATION_TIMEOUT_SECONDS` | `30` | Per-command shell timeout. |
| `VERIFICATION_ENABLED` | `true` | Enables metric polling after an action actually executes. |
| `VERIFICATION_TOTAL_MINUTES` | `10` | Maximum verification duration. |
| `VERIFICATION_POLL_SECONDS` | `30` | Delay between verification polls. |
| `LOG_LEVEL` | `INFO` | Application log level. |
| `OTEL_SERVICE_NAME` | `incident-response` | OpenTelemetry service name. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Enables OTLP HTTP export when the optional `otel` dependencies are installed. |

To use real integrations, add values like these to `.env`:

```dotenv
LLM_MODE=anthropic
ANTHROPIC_API_KEY=sk-ant-...

GITHUB_MODE=rest
GITHUB_TOKEN=...
GITHUB_REPO=owner/repo

SLACK_MODE=bot
SLACK_BOT_TOKEN=xoxb-...

METRICS_MODE=datadog
DATADOG_API_KEY=...
DATADOG_APP_KEY=...
```

## Incident Flow

1. `POST /alerts` receives an alert.
2. Auth accepts either the shared token or one configured HMAC signature.
3. The sliding-window rate limiter checks `(client_ip, service)`.
4. Dedup fingerprints `(service, metric, severity, 15-minute bucket)`.
5. The API returns `202` and submits the alert to the async worker.
6. The orchestrator opens an incident and persists it to SQLite.
7. Triage runs three agents in parallel:
   - Commit suspect ranking.
   - Runbook match.
   - User impact estimate.
8. The Slack brief is updated as partial results arrive.
9. The top suspect PR is annotated when confidence is high enough.
10. Matched runbook actions are dry-run by default, or executed in shell mode if explicitly allowed.
11. Verification polls metrics after executed remediation.
12. `POST /alerts/{id}/resolve` marks the incident resolved and writes a post-mortem.

## Runbooks

Runbooks are Markdown files in `./runbooks`. Frontmatter drives search. An
optional `## Automated actions` JSON block declares remediation steps.

````markdown
---
title: Checkout service elevated error rate
tags: [checkout, http_5xx]
---

## First actions
1. Confirm the alert in Datadog.
2. Check the last 3 deploys.

## Automated actions
```json
[
  {
    "name": "flip pricing cache off",
    "command": "feature-flag set checkout.pricing_cache off",
    "auto": true
  },
  {
    "name": "rollback last deploy",
    "command": "deploy rollback checkout --confirm"
  }
]
```
````

Execution rules:

- `REMEDIATION_MODE=mock` never touches the system. It returns `dry_run`.
- `REMEDIATION_MODE=shell` only considers steps with `"auto": true`.
- Shell mode only runs commands whose first token is in `REMEDIATION_ALLOWED_COMMANDS`.
- Skipped steps are reported as `skipped_not_auto` or `skipped_not_allowed`.
- Results are posted as a Slack thread reply and persisted in the incident timeline.

## Safety Model

The default configuration is intentionally non-destructive:

- Mock integrations are the default for GitHub, Slack, metrics, and remediation.
- `LLM_MODE=mock` provides a full offline path.
- `REMEDIATION_MODE=mock` dry-runs every automated action.
- Shell remediation requires both `"auto": true` in the runbook and an allow-listed command prefix.
- PR annotation failures are logged but never block incident handling.
- Post-mortem generation falls back to a deterministic template if the LLM output is invalid.
- SQLite is updated after every major incident step.

Important production caveats:

- The worker queue is in memory. A hard kill can lose queued but unprocessed alerts.
- Rate limit and dedup state are in memory. Use Redis or similar storage for multiple instances.
- Real LLM mode makes three calls per incident plus one post-mortem call on resolve.
- Human approval for remediation is not implemented yet.

## Storage And History

`IncidentStore` keeps one JSON blob per incident in SQLite. The incident record
contains:

- original alert
- current status
- triage report
- Slack message timestamp
- post-mortem path
- timeline events
- verification outcome

The API can return recent incidents with `GET /incidents`, ordered by newest
`created_at` first. That endpoint is the read model intended for local consoles
and operational dashboards. `IncidentStore.list_recent()` applies any status
filter in SQLite before applying `LIMIT`, so filtered requests do not drop older
matching incidents from the result window.

Generated post-mortems include a metadata footer with runbook and verification
status when available. `history.py` reads past post-mortems and boosts matches
where the same runbook previously recovered the system.

## Development

Install with Python 3.11 or newer and the development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run tests:

```bash
pytest
```

Current suite:

```text
140 passed, no network required
```

Feature-level TDD evidence is recorded in [`docs/testing/`](docs/testing/).

Run lint:

```bash
ruff check .
```

Useful smoke checks:

```bash
incident-response --help
incident-response demo
LLM_MODE=mock incident-response serve --host 127.0.0.1 --port 8080
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://localhost:8080/console
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
  http://localhost:8080/console/incidents/inc-ddg-9273
curl -i -X POST http://localhost:8080/console/demo-alert
curl -i -X POST \
  --data-urlencode "resolution_note=rolled back the pricing cache" \
  http://localhost:8080/console/incidents/inc-ddg-9273/resolve
```

## Project Layout

```text
src/incident_response/
  cli.py               CLI for serve and offline demo
  main.py              FastAPI app, lifespan worker, auth, rate limit, dedup
  console.py           Server-rendered operator console (HTML, no template engine)
  orchestrator.py      Alert -> triage -> brief -> remediate -> resolve -> post-mortem
  models.py            Pydantic domain models
  db.py                SQLite persistence
  config.py            Env-driven settings
  queue.py             In-process async worker
  dedup.py             Alert fingerprinting and TTL LRU
  rate_limit.py        Sliding-window rate limiter
  security.py          Datadog, PagerDuty, and generic HMAC verification
  retry.py             Exponential backoff with jitter
  executor.py          Mock and shell remediation executors
  history.py           Past post-mortem retrieval
  pr_annotation.py     Deterministic PR comment composer
  verification.py      Post-remediation metric polling
  runbooks_loader.py   Markdown frontmatter parser
  logging_config.py    JSON logs with incident and trace correlation
  telemetry.py         Optional OpenTelemetry setup
  agents/
    llm.py             AnthropicLLM, DemoLLM, FakeLLM, JSON extraction
    triage.py          Suspect commit ranking
    runbook.py         Runbook selection
    impact.py          User impact estimate
    brief.py           Slack brief composition
    postmortem.py      Post-mortem generation
  integrations/
    github.py          Mock and REST GitHub clients
    slack.py           Mock, webhook, and bot-token Slack clients
    metrics.py         Mock and Datadog metrics clients
  static/
    console.css        Console stylesheet, served at /static

tests/                 Pytest suite
runbooks/              Example runbooks
postmortems/           Runtime output directory, created on first resolve
```

## Extending It

Add a new integration by implementing the matching interface and updating the
factory:

- Git provider: `integrations/github.py`
- Chat provider: `integrations/slack.py`
- Metrics provider: `integrations/metrics.py`
- Remediation executor: `executor.py`

Add a new runbook by dropping a Markdown file into `RUNBOOKS_DIR` with useful
frontmatter tags and, optionally, a JSON `## Automated actions` block.

## Current Limits

- No durable queue yet.
- No Redis-backed rate limit or dedup for multi-instance deployments.
- No incident merging across services.
- No on-call rotation lookup.
- No Jira or Linear ticket creation.
- No human approval workflow for shell remediation.
- No provider-specific alert normalization beyond the shared alert schema.
- The console is local-first and has no authentication, RBAC, or approval gates. It
  exposes full incident detail to anyone who can reach the port. Console writes
  are restricted to demo creation and incident resolution in all-mock mode;
  real-integration resolution and remediation controls remain outside the console.
  The CLI defaults to `0.0.0.0`, so pass `--host 127.0.0.1` unless you have placed
  the service behind appropriate network and authentication controls.
- The console does not auto-refresh. Reload to see triage progress.
