# Durable Alert Queue Slice 2 TDD Evidence

## Source

No separate source plan was provided. This is the second small durable queue
slice documented in [`PLAN.md`](../../PLAN.md).

## User journeys

- As an operator, I want transient handler failures retried automatically, so
  that recovery does not require restarting the service.
- As an operator restarting the service, I want retry attempts and due times
  preserved, so that a restart neither loses history nor retries too early.
- As an existing deployment, I want the slice-1 queue table migrated in place,
  so that upgrading does not require deleting the database.

## RED evidence

```text
.venv/bin/pytest -q tests/test_queue.py
4 failed, 5 passed
```

The failures proved there was no retry configuration or backoff function, no
persisted schedule, and no migration for existing queue tables.

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Retry delay doubles by attempt and stops at the configured cap | `test_retry_delay_is_exponential_and_capped` | unit | PASS |
| 2 | Failed work retries automatically and is deleted after success | `test_durable_queue_retries_automatically_after_persisted_backoff` | async integration | PASS |
| 3 | Attempt count, last error, and due time survive restart | `test_retry_schedule_and_attempt_count_survive_restart` | restart integration | PASS |
| 4 | Restart does not execute work before its persisted due time | same target | timing regression | PASS |
| 5 | A slice-1 queue table gains all retry columns in place | `test_durable_queue_migrates_slice_one_schema` | migration | PASS |

Validation:

```text
.venv/bin/pytest -q tests/test_queue.py
9 passed

.venv/bin/pytest -q tests/test_queue.py tests/test_webhook.py \
  tests/test_webhook_auth.py tests/test_console.py tests/test_demo_mode.py \
  tests/test_logging.py
62 passed

.venv/bin/pytest -q
164 passed

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
164 passed
TOTAL 2118 statements, 250 missed, 88% coverage
```

## Known gaps

Retries are unbounded. Processing leases, maximum-attempt enforcement,
dead-letter handling, and atomic cross-process job claims remain future slices.
Deploy one active queue worker per SQLite database until job claiming ships.

## Merge evidence

- RED checkpoint: `3134042 test: define durable queue retry schedule`
- GREEN checkpoint: `f7d1ad9 feat: retry durable alerts with persisted backoff`
