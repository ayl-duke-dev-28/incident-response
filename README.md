# Autonomous Incident Response

Autonomous Incident Response turns a production alert into an investigation brief,
candidate root cause, matched runbook, safe remediation summary, and post-mortem.
It is built as a local-first FastAPI service with deterministic mock adapters and
a production profile backed by PostgreSQL, Redis, OIDC, PagerDuty, Jira, and
Linear.

## Production Readiness

The production profile now supports multi-instance operation with PostgreSQL
storage and Redis coordination, generic OIDC authentication with role-based
access control, CSRF protection, hashed bearer tokens, provider-specific alert
normalization and correlation, PagerDuty on-call lookup, Jira and Linear ticket
creation through a transactional outbox, and authenticated server-sent console
updates. Secure headers and loopback-first CLI defaults are enabled without
removing the zero-service local mock workflow.

<!-- AUTO-GENERATED: verification snapshot -->
Validated on 2026-08-08:

- `pytest`: 239 tests passed with 86% coverage and no network required.
- `ruff check .`, Python bytecode compilation, and `git diff --check`: clean.
- Browser QA covered demo creation, live `EventSource` updates, completed triage,
  remediation rejection, persisted on-call ownership, and persisted ticket
  references with no console errors.
- The offline CLI demo completed the alert-to-post-mortem path.
<!-- END AUTO-GENERATED: verification snapshot -->

Deployment-specific validation against live PostgreSQL, Redis, OIDC, PagerDuty,
Jira, and Linear instances still requires the target environment and its
credentials. See [Production Architecture](docs/production-architecture.md) for
the runtime topology and failure model.

## What You Can Do

- Receive normalized Datadog, PagerDuty, or generic webhook alerts through
  provider-specific, signature-checked endpoints.
- Authenticate webhooks with a shared token or HMAC signatures.
- Persist accepted alerts before returning `202`, using SQLite locally or
  PostgreSQL in production, then process them in a background worker.
- Recover unfinished alerts when the service restarts.
- Coordinate PostgreSQL workers with renewable leases and `SKIP LOCKED` claims.
- Retry failed alert handling automatically with persisted exponential backoff,
  then dead-letter alerts that exhaust the configured attempt limit.
- Inspect and replay dead-lettered alerts through authenticated operator APIs.
- Atomically deduplicate provider retries and merge related cross-service alerts
  by provider incident key, explicit correlation key, or service/metric/environment.
- Resolve and persist the current PagerDuty on-call responder and schedule.
- Create Jira and Linear incident tickets through a leased transactional outbox
  with stable idempotency keys, bounded retries, and dead-letter state.
- Triage recent commits, match the best runbook, and estimate user impact in parallel.
- Stream a Slack incident brief as each agent finishes.
- Annotate the suspect PR when confidence clears the configured floor.
- Review, approve, or reject persisted runbook actions before any executor runs.
- Dry-run or execute approved, allow-listed runbook actions.
- Verify whether remediation actually reduced the error rate.
- Persist incident state to SQLite after every major step.
- List recent incidents without knowing an incident ID.
- Inspect open and recently resolved incidents in an OIDC-authenticated console
  with viewer, responder, and admin roles.
- Leave any incident detail page open for authenticated SSE updates delivered
  through Redis across instances; the browser replaces content only on change.
- Open matched runbooks directly from incident list and detail views.
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

The CLI and console demo actions use the same typed checkout scenario from
`incident_response.demo`:

- `build_demo_alert()` owns the shared title, service, severity, threshold, value,
  and default tags. Its defaults preserve the deterministic CLI alert ID,
  timestamp, and metric shown above.
- `build_unique_console_demo_alert()` reuses that scenario while supplying a
  unique alert ID, metric, current timestamp, and `source=console` tag for each
  browser demo.

Change the built-in demo scenario in `demo.py` instead of copying alert payloads
into the CLI, console, or tests. The behavior is covered by the
[shared demo alert TDD evidence](docs/testing/shared-demo-alert.tdd.md).

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

If the matched runbook proposes actions, the incident contains a `remediation`
object with `status: "pending"` and the exact commands awaiting review. Approve
that immutable proposal through the authenticated API:

```bash
curl -X POST \
  http://localhost:8080/alerts/inc-ddg-9273/remediation/approve \
  -H "x-webhook-token: change-me" \
  -H "content-type: application/json" \
  -d '{"decided_by":"alice@example.com","note":"Approved by primary on-call"}'
```

Or reject it without invoking the executor:

```bash
curl -X POST \
  http://localhost:8080/alerts/inc-ddg-9273/remediation/reject \
  -H "x-webhook-token: change-me" \
  -H "content-type: application/json" \
  -d '{"decided_by":"incident-commander","note":"Use manual failover instead"}'
```

List recent incidents:

```bash
curl http://localhost:8080/incidents
```

Filter or limit the list:

```bash
curl "http://localhost:8080/incidents?status=investigating&limit=10"
```

Inspect alerts that exhausted their retry limit:

```bash
curl http://localhost:8080/dead-letters \
  -H "x-webhook-token: change-me"
```

Replay one exhausted alert:

```bash
curl -X POST http://localhost:8080/dead-letters/ddg-9273/replay \
  -H "x-webhook-token: change-me"
```

Response:

```json
{"status":"replayed","incident_id":"inc-ddg-9273"}
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
While triage continues in the background, the detail page shows a visible status
message and opens an authenticated EventSource connection. Redis carries change
notifications across instances, and the browser replaces the rendered content
only when the persisted incident version changes. You can also populate the
console by sending the test alert from the API section above.

Select an incident title to open `/console/incidents/{id}`. The detail page shows:

- alert description, service, metric, threshold, current value, and tags;
- triage summary, estimated impact, matched runbook, and suspect commits;
- the exact remediation commands awaiting approval and their decision history;
- the remediation and resolution timeline;
- verification results and the generated post-mortem path when available.

Matched runbook names link to `/console/runbooks/{slug}`. The preview shows the
loaded title, tags, instructions, and automated-action declarations as escaped
Markdown source. URL slugs are matched only against runbooks loaded at startup;
they are never resolved as filesystem paths.

Incidents still being triaged render an in-progress state instead of incomplete
sections. Open, completed, and resolved incident pages all retain the SSE stream.
Once triage finishes, authorized responders see a **Resolve incident** form.
Submit an optional note of up to 500 characters to mark the incident resolved,
generate its post-mortem, and return to the updated detail page. Unknown incident
IDs return a navigable HTML `404` page.

When a runbook proposes remediation, authorized admins see **Approve and run**;
responders can **Reject**. Both accept an optional 500-character decision note.
The signed session, role, and CSRF token are checked before the final decision is
persisted. OIDC identity, rather than form input, is recorded as the actor.

The demo button is shown only when LLM, GitHub, Slack, metrics, and remediation
modes are all `mock`. It is hidden and `POST /console/demo-alert` returns `403` if
any mode could reach a real integration or execute shell remediation. Cross-site
browser submissions are also rejected.

In development with authentication disabled, console writes remain limited to
the all-mock, same-origin workflow. In production, OIDC and RBAC enable the same
resolution and remediation controls with CSRF protection.

What works today:

| Surface | Status |
|---|---|
| `GET /console` incident list | Working. |
| `GET /console/incidents/{id}` incident detail | Working. Shows alert, triage, impact, runbook, suspect, ownership, ticket references, timeline, verification, and resolution data with SSE updates. |
| `GET /console/runbooks/{slug}` runbook preview | Working. Shows escaped Markdown for an already-loaded runbook and returns an HTML `404` for unknown slugs. |
| `GET /static/console.css` and `console.js` | Working under the production CSP. |
| `POST /console/demo-alert` demo action | Working in all-mock mode. Enqueues a unique demo incident and redirects to its detail page. Hidden and forbidden when any integration or remediation mode is not `mock`. |
| Console remediation approval | Working in all-mock development and with OIDC admin/responder roles in production. |
| `POST /console/incidents/{id}/resolve` resolve action | Working after triage for all-mock development or OIDC responders. |

SSE rendering has integration coverage for pending, completed, and resolved
incidents. Browser QA exercised the empty state, demo creation, SSE connection,
remediation decision, and ticket/on-call persistence with no console errors. See the
[console triage auto-refresh TDD evidence](docs/testing/console-triage-auto-refresh.tdd.md)
for the exact test commands and results.

Development defaults to local, unauthenticated operation. Production refuses to
start without OIDC, secure signed sessions, PostgreSQL, TLS Redis, and strong
webhook credentials.

### Production identity and roles

OIDC group claims map to `viewer`, `responder`, and `admin`. Viewers can read;
responders can resolve and reject; admins can also approve remediation and replay
dead letters. Browser writes require the signed session plus a constant-time CSRF
token. Automation can use `Authorization: Bearer ...`; configure only SHA-256
digests and roles in `OPERATOR_BEARER_TOKENS`, never plaintext API tokens.

The browser session contains identity, role, expiry, and CSRF state only—not OIDC
access or refresh tokens. Responses include CSP, frame denial, MIME sniffing and
referrer controls; production also emits HSTS.

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
| `incident-response serve` | Start the FastAPI server. Defaults to `127.0.0.1:8080`; pass `--host` explicitly to bind another interface. |

Useful demo flags:

| Flag | Default | Purpose |
|---|---|---|
| `--db-path` | `./demo-incidents.db` | SQLite file for demo incident state. |
| `--postmortem-dir` | `./demo-postmortems` | Directory for generated post-mortems. |
| `--runbooks-dir` | `./runbooks` | Runbook library used by the demo. |
| `--webhook-token` | `demo-secret` | Token used by the in-process demo request. |
| `--timeout-seconds` | `5.0` | Max time to wait for async triage. |

## API

<!-- AUTO-GENERATED: routes from src/incident_response/main.py and console.py -->

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/alerts` | Validate generic webhook credentials, persist an alert, and return `202`. |
| `POST` | `/alerts/datadog` | Validate the Datadog signature and normalize a Datadog alert. |
| `POST` | `/alerts/pagerduty` | Validate the PagerDuty signature and normalize a v3 webhook event. |
| `GET` | `/incidents` | List recent incidents. Requires `viewer`; supports `status` and `limit`. |
| `GET` | `/incidents/{id}` | Fetch current incident state. Requires `viewer`. |
| `GET` | `/dead-letters` | List exhausted alerts. Requires `viewer`. |
| `POST` | `/dead-letters/{alert_id}/replay` | Return one exhausted alert to the queue. Requires `admin` and CSRF for sessions. |
| `POST` | `/alerts/{id}/remediation/approve` | Approve and execute a persisted proposal. Requires `admin`. |
| `POST` | `/alerts/{id}/remediation/reject` | Reject a persisted proposal. Requires `responder`. |
| `POST` | `/alerts/{id}/resolve` | Resolve an incident and generate a post-mortem. Requires `responder`. |
| `GET` | `/events/incidents/{id}` | Authenticated SSE stream with version events and heartbeats. Requires `viewer`. |
| `GET` | `/healthz` | Liveness check. |
| `GET` | `/readyz` | Liveness plus the number of persisted unfinished alerts in `queue_depth`. |
| `GET` | `/console` | Operator incident list. Requires `viewer` when OIDC is enabled. |
| `GET` | `/console/incidents/{id}` | Live operator incident detail. Requires `viewer`. |
| `GET` | `/console/runbooks/{slug}` | Escaped runbook preview. Requires `viewer`. |
| `POST` | `/console/demo-alert` | Enqueue a unique checkout demo and redirect to its detail page. Available only when all integrations and remediation use `mock`. |
| `POST` | `/console/incidents/{id}/remediation/approve` | Approve a proposal. Requires `admin` plus CSRF. |
| `POST` | `/console/incidents/{id}/remediation/reject` | Reject a proposal. Requires `responder` plus CSRF. |
| `POST` | `/console/incidents/{id}/resolve` | Resolve a triaged incident. Requires `responder` plus CSRF. |
| `GET` | `/static/console.css` | Console stylesheet. |
| `GET` | `/static/console.js` | CSP-compatible EventSource client for live incident updates. |

<!-- END AUTO-GENERATED -->

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

Dead-letter list query params:

| Param | Default | Notes |
|---|---:|---|
| `limit` | `50` | Maximum dead letters to return. Must be between `1` and `200`; out-of-range values return `422`. |

`GET /dead-letters` returns the original alert plus its final `attempt_count`,
`last_error`, and `failed_at` timestamp. Results are ordered by failure time,
newest first. Listing is read-only and does not delete or requeue rows.

`POST /dead-letters/{alert_id}/replay` moves one dead letter back to the active
queue, resets its attempt count and retry schedule, clears its final error, and
wakes the worker. It returns `202` with the incident ID. Missing dead letters
return `404`; an alert ID already present in the active queue returns `409`
without changing either row.

## Durable Alert Queue

<!-- AUTO-GENERATED: durable queue behavior from src/incident_response/queue.py and main.py -->

The FastAPI app stores queue rows in the same SQLite file configured by
`DB_PATH`. `POST /alerts` completes that insert before returning `202 Accepted`.
Each row contains the provider alert ID, serialized alert payload, and enqueue
time.

Queue lifecycle:

1. `submit()` inserts the alert with `INSERT OR IGNORE`; one pending alert ID
   occupies one row.
2. A worker wakes immediately for new submissions and polls SQLite for due rows
   after startup.
3. Before handling an alert, the worker atomically claims it with a unique token
   and a `lease_expires_at` timestamp. Other workers skip the row while that
   lease is active; an abandoned row becomes claimable after expiry.
4. While handling continues, the worker renews its lease every one-third of the
   configured duration. Each renewal requires the current ownership token.
5. Successful handling deletes the row only when the worker still owns its
   lease. A stale worker cannot delete work that another worker reclaimed.
6. Failed handling with the current lease increments `attempt_count`, stores up
   to 500 characters of `last_error`, schedules `next_attempt_at`, and releases
   the lease.
7. The delay is `base × 2^(attempt-1)`, capped by the configured maximum. The
   worker polls SQLite and retries the row when it becomes due.
8. When `attempt_count` reaches `QUEUE_MAX_ATTEMPTS`, the same transaction copies
   the payload, final error, attempt count, and failure timestamp into
   `alert_dead_letters` and deletes the active queue row.
9. Restarting the service preserves retry schedules, active leases, and dead
   letters. Abandoned leased work recovers after its persisted expiry.

`GET /readyz` and the console queue indicator count persisted rows, including
scheduled retries and work currently being handled but not yet completed. Dead
letters are excluded from this active depth and are not polled again. Direct
`AlertQueue` users that omit `db_path` still get the original in-memory-only
behavior; the FastAPI application always supplies `Settings.db_path`.

Authenticated operators can inspect exhausted work with `GET /dead-letters`.
The endpoint returns the original alert, final attempt count, last stored error,
and failure timestamp, ordered by failure time with the newest first. Its
optional `limit` defaults to 50 and accepts values from 1 through 200. Listing
does not modify durable storage or active queue depth. See the
[dead-letter listing TDD evidence](docs/testing/durable-alert-queue-slice-4.tdd.md)
for the tested contract.

Authenticated operators can replay one row with
`POST /dead-letters/{alert_id}/replay`. The database transition is atomic: it
inserts a fresh active row with retry state reset and deletes the dead letter in
the same transaction. The service then clears in-memory deduplication state and
wakes the worker. A conflict with an existing active row leaves both rows
unchanged. See the
[dead-letter replay TDD evidence](docs/testing/durable-alert-queue-slice-5.tdd.md)
for the tested contract.

Multiple workers may share one SQLite database. Claims use SQLite write
transactions, and completion or failure updates require the current lease token.
`QUEUE_LEASE_SECONDS` defaults to 300 seconds, and an active worker renews its
claim every one-third of that duration. Renewal is best-effort: database errors
or event-loop stalls lasting through the lease window can still let another
worker repeat external calls, although token checks prevent a stale worker from
deleting, rescheduling, or dead-lettering the reassigned row. See the
[processing lease TDD evidence](docs/testing/durable-alert-queue-slice-6.tdd.md)
for the concurrency and crash-recovery contract and the
[lease heartbeat TDD evidence](docs/testing/durable-alert-queue-slice-7.tdd.md)
for long-running handler coverage.

<!-- END AUTO-GENERATED -->

## Configuration

All runtime settings are loaded from environment variables or `.env`. The values
below are the defaults unless the row says a credential is required.

<!-- AUTO-GENERATED: settings from src/incident_response/config.py and .env.example -->

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
| `DB_PATH` | `./incidents.db` | Shared SQLite file for incidents and durable alert queue rows. |
| `ENVIRONMENT` | `development` | `production` enables fail-closed validation. |
| `DATABASE_URL` | empty | Production PostgreSQL URL using `postgresql+psycopg://`. |
| `REDIS_URL` | empty | Production Redis URL; `rediss://` is required when TLS enforcement is enabled. |
| `REDIS_NAMESPACE` | `incident-response` | Prefix for rate-limit, dedup, and incident-event keys/channels. |
| `REDIS_REQUIRE_TLS` | `true` | Reject plaintext production Redis URLs. |
| `QUEUE_RETRY_BASE_SECONDS` | `1` | Delay before the first automatic queue retry. |
| `QUEUE_RETRY_MAX_SECONDS` | `60` | Maximum exponential queue retry delay. Must be at least the base delay. |
| `QUEUE_MAX_ATTEMPTS` | `5` | Handler failures allowed before an alert is moved to `alert_dead_letters`. Must be positive. |
| `QUEUE_LEASE_SECONDS` | `300` | Queue ownership duration. Must be positive; active handlers renew every one-third of this interval. |
| `AUTH_MODE` | `disabled` | `oidc` is required in production. |
| `SESSION_SECRET` | empty | Signed-session secret; at least 32 characters in production. |
| `SESSION_SECONDS` | `28800` | Absolute application-session lifetime. |
| `SESSION_HTTPS_ONLY` | `true` | Secure-cookie flag; required in production. |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | empty | OIDC authorization-code client credentials. |
| `OIDC_METADATA_URL` | empty | HTTPS discovery document URL. |
| `OIDC_GROUPS_CLAIM` | `groups` | Userinfo claim containing role groups. |
| `OIDC_VIEWER_GROUPS` | `incident-viewers` | Comma-separated viewer groups. |
| `OIDC_RESPONDER_GROUPS` | `incident-responders` | Comma-separated responder groups. |
| `OIDC_ADMIN_GROUPS` | `incident-admins` | Comma-separated admin groups. |
| `OPERATOR_BEARER_TOKENS` | empty | JSON mapping lowercase SHA-256 token digests to roles. |
| `WEBHOOK_TOKEN` | `change-me` | Generic shared token; production rejects the default and short values. |
| `DATADOG_WEBHOOK_SECRET` | empty | Datadog HMAC secret; required in production. |
| `PAGERDUTY_WEBHOOK_SECRET` | empty | PagerDuty HMAC secret; required in production. |
| `GENERIC_WEBHOOK_SECRET` | empty | Optional generic HMAC-SHA256 secret. |
| `PAGERDUTY_MODE` | `mock` | `mock`, `disabled`, or `pagerduty`. Production forbids implicit mock mode. |
| `PAGERDUTY_API_TOKEN` | empty | REST API token for service and on-call lookup. |
| `PAGERDUTY_SERVICE_IDS` | empty | JSON mapping internal services to PagerDuty service IDs. |
| `JIRA_MODE` | `mock` | `mock`, `disabled`, or `jira`. |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | empty | Jira Cloud connection and basic-auth credentials. |
| `JIRA_PROJECT_KEY` / `JIRA_ISSUE_TYPE` | empty / `Incident` | Destination project and issue type. |
| `LINEAR_MODE` | `mock` | `mock`, `disabled`, or `linear`. |
| `LINEAR_API_TOKEN` / `LINEAR_TEAM_ID` | empty | Linear API key and destination team. |
| `OUTBOX_RETRY_BASE_SECONDS` | `1` | Initial ticket delivery retry delay. |
| `OUTBOX_RETRY_MAX_SECONDS` | `60` | Maximum ticket delivery retry delay. |
| `OUTBOX_MAX_ATTEMPTS` | `5` | Attempts before ticket delivery is dead-lettered. |
| `OUTBOX_LEASE_SECONDS` | `60` | Cross-process outbox claim duration. |
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

<!-- END AUTO-GENERATED -->

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

A production deployment must additionally select real or explicitly disabled
operator integrations and provide shared infrastructure and identity:

```dotenv
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://incident:...@postgres/incident_response
REDIS_URL=rediss://redis:6379/0

AUTH_MODE=oidc
SESSION_SECRET=<32-or-more-random-characters>
OIDC_CLIENT_ID=incident-response
OIDC_CLIENT_SECRET=...
OIDC_METADATA_URL=https://identity.example/.well-known/openid-configuration

WEBHOOK_TOKEN=<32-or-more-random-characters>
DATADOG_WEBHOOK_SECRET=<32-or-more-random-characters>
PAGERDUTY_WEBHOOK_SECRET=<32-or-more-random-characters>

PAGERDUTY_MODE=pagerduty
PAGERDUTY_API_TOKEN=...
PAGERDUTY_SERVICE_IDS={"checkout":"PSERVICE123"}
JIRA_MODE=jira
JIRA_BASE_URL=https://company.atlassian.net
JIRA_EMAIL=incidents@example.com
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=OPS
LINEAR_MODE=linear
LINEAR_API_TOKEN=lin_api_...
LINEAR_TEAM_ID=...
```

## Incident Flow

1. `POST /alerts` receives an alert.
2. Auth accepts either the shared token or one configured HMAC signature.
3. The sliding-window rate limiter checks `(client_ip, service)`.
4. Dedup fingerprints `(service, metric, severity, 15-minute bucket)`.
5. The API persists the alert to SQLite, returns `202`, and wakes the async worker.
6. The orchestrator opens an incident and persists it to SQLite.
7. Triage runs three agents in parallel:
   - Commit suspect ranking.
   - Runbook match.
   - User impact estimate.
8. The Slack brief is updated as partial results arrive.
9. The top suspect PR is annotated when confidence is high enough.
10. Matched runbook actions are persisted as an immutable approval proposal.
11. An authenticated operator approves or rejects the proposal.
12. Approved actions are dry-run by default, or executed in shell mode if explicitly allowed.
13. Verification polls metrics after executed remediation.
14. `POST /alerts/{id}/resolve` marks the incident resolved and writes a post-mortem.

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

- No runbook action reaches an executor until an operator approves it.
- Approval executes the exact persisted proposal, even if the runbook changes later.
- Approval or rejection is final; duplicate decisions return `409 Conflict`.
- SQLite serializes the pending-to-decided transition, so processes sharing the
  same database cannot both execute one proposal.
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
- Every remediation proposal requires an authenticated, persisted approval.
- Shell remediation requires both `"auto": true` in the runbook and an allow-listed command prefix.
- PR annotation failures are logged but never block incident handling.
- Post-mortem generation falls back to a deterministic template if the LLM output is invalid.
- SQLite is updated after every major incident step.

Important production caveats:

- Pending alerts survive process restarts in SQLite. Failures retry automatically
  with a persisted attempt count and exponential delay capped at 60 seconds by
  default. After five failures by default, the alert moves to durable dead-letter
  storage and leaves active queue depth.
- Queue workers coordinate through persisted claims and renew active leases
  every one-third of `QUEUE_LEASE_SECONDS`. Keep the lease comfortably above
  expected database and event-loop stalls; missed heartbeats can still allow
  duplicate external calls after expiry.
- Rate limit and dedup state are in memory. Use Redis or similar storage for multiple instances.
- Real LLM mode makes three calls per incident plus one post-mortem call on resolve.
- Approval and execution completion use atomic SQLite transactions. Concurrent
  processes sharing the same database produce one decision winner, and completion
  preserves incident changes written by another process.

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
- remediation proposal, operator decision, and execution summary

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
239 passed, 86% coverage, no network required
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
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
  http://localhost:8080/console/runbooks/checkout-error-rate
curl -i -X POST http://localhost:8080/console/demo-alert
curl -i -X POST \
  --data-urlencode "resolution_note=rolled back the pricing cache" \
  http://localhost:8080/console/incidents/inc-ddg-9273/resolve
```

## Project Layout

```text
src/incident_response/
  auth.py              OIDC sessions, RBAC, CSRF, and hashed bearer tokens
  cli.py               Loopback-first server CLI and offline demo
  config.py            Validated development and production settings
  console.py           Authenticated server-rendered operator console
  db.py                SQLite persistence and storage interfaces
  dedup.py             Local alert fingerprinting and TTL deduplication
  demo.py              Shared typed alert scenario for CLI and console demos
  events.py            Local and Redis-backed incident event publishing
  main.py              FastAPI routes, lifespan workers, and dependency wiring
  models.py            Pydantic domain models
  normalization.py     Datadog, PagerDuty, and generic alert normalization
  orchestrator.py      Alert -> triage -> brief -> remediate -> post-mortem
  outbox.py            Durable, leased Jira and Linear delivery worker
  postgres.py          PostgreSQL incidents, queue, correlation, and outbox
  queue.py             Durable alert queue and renewable worker leases
  rate_limit.py        Local and Redis-backed rate limiting
  security.py          Webhook signature verification and security headers
  executor.py          Mock and allow-listed shell remediation executors
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
    metrics.py         Mock and Datadog metrics clients
    on_call.py         Mock and PagerDuty on-call lookup
    slack.py           Mock, webhook, and bot-token Slack clients
    tickets.py         Mock, Jira, and Linear ticket clients
  static/
    console.css        Console stylesheet, served at /static
    console.js         Authenticated SSE console updates

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

- PostgreSQL, Redis, and OIDC are mandatory only in the production profile. The
  development profile intentionally keeps SQLite and local identity for a
  zero-service demo.
- Jira and Linear ticket creation is transactional on the application side, but
  no system can promise exactly-once behavior after an ambiguous provider timeout
  unless that provider honors or exposes a matching idempotency lookup. Stable
  keys, persisted references, leases, and bounded retries minimize duplicates.
- Slack brief streaming, GitHub annotation, and remediation commands still occur
  inside the leased alert workflow rather than the ticket outbox. Lease renewal
  and token-checked completion reduce stale work but cannot make arbitrary
  external side effects mathematically exactly once.
- PagerDuty service ownership uses an explicit internal-service-to-PagerDuty-ID
  mapping. It does not discover that mapping from a service catalog.
- Provider normalization currently covers Datadog, PagerDuty v3, and the generic
  schema. Additional alert vendors need a normalizer and signature verifier.
- Jira and Linear adapters create incident tickets and persist their references;
  they do not yet synchronize later incident timeline or resolution changes back
  to those tickets.
