# Durable Alert Queue Slice 6 TDD Evidence

## Source

No separate source plan was provided. The journeys were derived for the sixth
small durable queue slice and recorded in [`PLAN.md`](../../PLAN.md).

## User journeys

- As an operator running multiple workers, I want each due alert claimed by only
  one worker, so that normal concurrent polling does not duplicate processing.
- As an operator recovering from a worker crash, I want abandoned work hidden
  until its lease expires and claimable afterward.
- As an operator with a slow stale worker, I want completion and failure writes
  ownership-checked, so that they cannot corrupt a row reassigned to another
  worker.
- As an operator upgrading an existing database, I want lease columns added in
  place without deleting queued work.

## RED evidence

```text
.venv/bin/pytest -q tests/test_queue.py
6 failed, 12 passed
```

The failures proved that `AlertQueue` did not accept or validate a lease
duration, existing schemas lacked lease columns, and workers had no atomic claim
or stale-owner protection.

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Existing queue tables gain ownership-token and expiry columns in place | `test_durable_queue_migrates_slice_one_schema` | migration | PASS |
| 2 | Two workers sharing one database normally invoke the handler once | `test_two_workers_claim_one_alert_only_once` | async concurrency integration | PASS |
| 3 | A crashed worker's row remains unavailable before expiry and recovers afterward | `test_expired_lease_recovers_work_abandoned_by_crashed_worker` | crash-recovery integration | PASS |
| 4 | A stale successful worker cannot delete a reassigned row | `test_stale_worker_cannot_mutate_alert_reassigned_after_lease_expiry[False]` | ownership integration | PASS |
| 5 | A stale failing worker cannot reschedule or dead-letter a reassigned row | `test_stale_worker_cannot_mutate_alert_reassigned_after_lease_expiry[True]` | ownership integration | PASS |
| 6 | Non-positive lease durations are rejected | `test_lease_seconds_must_be_positive` | unit | PASS |

Validation:

```text
.venv/bin/pytest -q tests/test_queue.py
18 passed

.venv/bin/pytest -q
182 passed, 1 warning

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
182 passed, 1 warning
TOTAL 2209 statements, 250 missed, 89% coverage
```

## Coverage and known gaps

The warning is the existing Starlette `TestClient` deprecation notice for its
httpx compatibility layer. Leases are fixed-duration and are not renewed while a
handler runs. Operators must configure `QUEUE_LEASE_SECONDS` above the longest
expected handler duration; otherwise a second worker may repeat external side
effects after expiry. Token-guarded queue mutations still prevent the stale
worker from deleting, rescheduling, or dead-lettering the reassigned row.

Rate limiting and alert deduplication remain in memory, so shared queue claims do
not by themselves make every application concern multi-instance safe.

## Merge evidence

- RED checkpoint: `a118700 test: define durable queue processing leases`
- GREEN checkpoint: `0965ec4 update`
