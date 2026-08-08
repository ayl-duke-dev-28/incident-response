# Durable Alert Queue Slice 7 TDD Evidence

## Source

No separate source plan was provided. The journeys were derived for the seventh
small durable queue slice and recorded in [`PLAN.md`](../../PLAN.md).

## User journeys

- As an operator with a long-running alert handler, I want its active claim
  renewed so another worker does not start the same alert when the original
  lease window passes.
- As an operator recovering from a crashed worker, I want heartbeat cancellation
  to leave the persisted lease to expire normally.
- As an operator with a stale worker, I want every queue mutation ownership
  checked so it cannot alter a row reclaimed under a new token.

## RED evidence

```text
.venv/bin/pytest -q tests/test_queue.py
1 failed, 18 passed
```

The new concurrency test held a handler beyond its 150 ms lease while a second
worker polled the same database. Both workers entered the handler, proving that
the fixed-duration claim expired during active processing.

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | A handler running beyond its original lease is not concurrently repeated | `test_active_worker_renews_lease_during_long_handler` | async concurrency integration | PASS |
| 2 | A canceled worker stops renewing and abandoned work recovers after expiry | `test_expired_lease_recovers_work_abandoned_by_crashed_worker` | crash-recovery integration | PASS |
| 3 | Stale success and failure paths cannot mutate a row held under a new token | `test_stale_worker_cannot_mutate_alert_reassigned_after_lease_expiry` | ownership integration | PASS |

Validation:

```text
.venv/bin/pytest -q tests/test_queue.py
19 passed

.venv/bin/pytest -q
183 passed, 1 warning

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
183 passed, 1 warning
TOTAL 2236 statements, 255 missed, 89% coverage
```

## Coverage and known gaps

The warning is the existing Starlette `TestClient` deprecation notice for its
httpx compatibility layer. The heartbeat runs every one-third of the configured
lease and stops before completion or failure is recorded. If SQLite renewal
raises an exception, the worker logs it and continues handling; ownership-token
checks still prevent stale durable mutations.

Renewal is best-effort rather than an exactly-once guarantee. Database
unavailability or event-loop starvation lasting through the lease window can
still allow another worker to invoke external integrations. Rate limiting and
alert deduplication also remain in memory across application instances.

## Merge evidence

- RED checkpoint: `f7e3627 test: define durable queue lease heartbeats`
- GREEN checkpoint: `50857c2`
