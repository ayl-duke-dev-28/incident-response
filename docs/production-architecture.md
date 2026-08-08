# Production Architecture

## Objective

Run multiple incident-response instances safely behind a load balancer while
preserving the existing local, deterministic mock workflow.

## Runtime profiles

| Profile | Durable database | Coordination | Operator identity | External systems |
|---|---|---|---|---|
| `development` | SQLite | in-process | disabled on loopback | mock by default |
| `production` | PostgreSQL | Redis | OIDC plus RBAC | explicitly configured |

Production startup fails closed when PostgreSQL, Redis, the OIDC session secret,
or OIDC discovery settings are absent. It never silently falls back to SQLite,
in-memory coordination, unauthenticated console access, or mock integrations.

## Identity and authorization

OIDC authorization-code login establishes a signed, HTTP-only, secure,
SameSite=Lax session. The session contains only the stable subject, display
identity, role, CSRF token, and expiry. OIDC group claims map to three roles:

| Role | Read console/API | Resolve/reject | Approve remediation | Replay dead letters | Admin configuration |
|---|---:|---:|---:|---:|---:|
| `viewer` | yes | no | no | no | no |
| `responder` | yes | yes | no | no | no |
| `admin` | yes | yes | yes | yes | yes |

All browser writes require both the signed session and a constant-time CSRF
token match. Operator API calls use separately configured, hashed bearer tokens
with an assigned role; inbound webhook credentials cannot authorize operator
actions. Authentication failures return `401`; insufficient roles return `403`.

## Persistence and coordination

PostgreSQL is authoritative for incidents, alert queue state, merge membership,
external references, and outbox delivery. Transactions use row locks or atomic
conditional updates for queue claims, incident merges, remediation decisions,
and outbox claims. Schema changes are versioned migrations.

Redis stores only reconstructable state:

- atomic sliding-window rate-limit keys;
- deduplication/correlation hints with TTLs;
- pub/sub notifications that wake SSE clients and workers.

Redis loss fails closed for inbound rate limiting in production, but does not
lose incidents or outbox work. The application rebuilds dedup hints from
PostgreSQL when necessary.

## Alert normalization and incident correlation

Provider endpoints validate signatures before parsing and normalize Datadog,
PagerDuty, and generic payloads into `Alert`. A normalized alert carries source,
provider event ID, provider incident key, and an explicit correlation key.

Correlation uses this priority:

1. provider incident key;
2. explicit `correlation_key` supplied by the sender;
3. normalized service + metric + environment identity.

An atomic PostgreSQL transaction attaches a matching alert to one open incident
inside the configured merge window. Resolved incidents are never reopened
implicitly. Provider event IDs are unique, making webhook retries idempotent.

## External integrations

PagerDuty on-call lookup resolves escalation-policy and schedule data for the
incident service. Jira Cloud and Linear adapters create configurable incident
tickets and persist their provider IDs and URLs. Every adapter has a deterministic
mock implementation and validates required production credentials at startup.

## Transactional outbox

The transaction that changes incident state also inserts an outbox row with a
stable idempotency key. Workers claim rows using `FOR UPDATE SKIP LOCKED`, renew
claims, retry with capped exponential backoff, and dead-letter exhausted events.
Provider IDs are recorded before an outbox row is marked delivered.

This prevents application-side lost writes and suppresses repeats where the
provider supports idempotency or lookup by a stable external key. It does not
claim mathematically exact-once delivery from providers that expose neither.

## Live console

Authenticated incident pages open an `EventSource` connection. The SSE endpoint
sends an initial incident version, heartbeat comments, and later version events
published through Redis. The browser fetches and replaces the incident fragment
only when its version changes. It reconnects automatically using `Last-Event-ID`
and falls back to a visible manual reload action if the stream is unavailable.

## Delivery sequence

1. PostgreSQL/Redis infrastructure and shared coordination.
2. OIDC sessions, bearer-token API auth, CSRF, and RBAC.
3. Provider normalization and atomic incident merging.
4. PagerDuty on-call and Jira/Linear ticket adapters.
5. Transactional outbox and external dispatch migration.
6. Authenticated SSE console and production console actions.
7. Security audit, migration tests, live QA, and documentation.

Each step preserves the development profile and lands through a failing test,
minimal implementation, full regression run, and recorded TDD evidence.
