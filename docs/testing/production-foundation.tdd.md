# Production Foundation TDD Evidence

## Source

The production baseline is defined in
[`docs/production-architecture.md`](../production-architecture.md). This slice
covers its PostgreSQL and Redis foundation only.

## User journeys

- As an operator running multiple instances, I want rate-limit and deduplication
  state shared through Redis.
- As a production deployer, I want startup to reject SQLite, missing Redis, or
  an insecure Redis URL instead of silently degrading.
- As a queue worker, I want PostgreSQL claims to skip rows another transaction
  has locked.
- As an incident responder, I want concurrent remediation decisions to lock and
  update the latest PostgreSQL incident row.
- As an operator, I want the application lifecycle to open, migrate, and close
  its PostgreSQL pool predictably.

## RED evidence

| Checkpoint | Intended failure |
|---|---|
| `ebb33ac` | Redis coordination classes did not exist. |
| `2001534` | `create_app` could not accept or select a shared Redis client. |
| `8537c9f` | The PostgreSQL persistence module did not exist. |
| `d1830c5` | The PostgreSQL pool lifecycle did not exist. |
| `fe0e9d4` | The production app could not select PostgreSQL stores. |

Each checkpoint was executed and failed at the named missing contract before
production code for that contract was added.

## GREEN evidence

| Guarantee | Test target | Result |
|---|---|---|
| Production requires PostgreSQL and secure Redis | `tests/test_production_config.py` | PASS |
| Redis rate limiting uses one atomic server-time script | `test_redis_rate_limit_uses_one_atomic_script_and_shared_namespace` | PASS |
| Redis dedup is shared, expiring, namespaced, and clearable | `test_redis_dedup_is_shared_expiring_namespaced_and_clearable` | PASS |
| Two app instances share a rate-limit decision | `test_two_app_instances_share_redis_rate_limit` | PASS |
| PostgreSQL schema changes are versioned | `test_postgres_migrations_are_versioned_and_create_all_foundation_tables` | PASS |
| Queue claims use `FOR UPDATE SKIP LOCKED` | `test_postgres_queue_claims_due_work_with_skip_locked` | PASS |
| Remediation decisions lock the latest row | `test_postgres_remediation_decision_locks_latest_incident_row` | PASS |
| App lifecycle owns PostgreSQL open/migrate/close | `test_production_app_uses_postgres_stores_and_manages_database_lifecycle` | PASS |

Validation:

```text
.venv/bin/pytest -q
196 passed, 1 warning

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
196 passed, 1 warning
TOTAL 2518 statements, 358 missed, 86% coverage
```

The warning is the existing Starlette `TestClient` deprecation notice.

## Known gaps

The deterministic suite validates PostgreSQL SQL and transaction contracts with
recording connections because no PostgreSQL daemon is available in the local
test environment. A deployment smoke test against a real PostgreSQL and Redis
service remains part of the final production validation phase. PostgreSQL module
coverage is 51% at this checkpoint; later outbox, correlation, and integration
slices exercise the remaining paths.

## GREEN checkpoints

- `c44b10c` shared Redis runtime wiring
- `757331b` PostgreSQL incident and queue stores
- `bec57d4` PostgreSQL pool lifecycle
- `dafba07` production PostgreSQL runtime selection
