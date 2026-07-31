# Durable Alert Queue Slice 1 TDD Evidence

## Source

No separate source plan was provided. This is the first small slice of the
durable queue milestone documented in [`PLAN.md`](../../PLAN.md).

## User journeys

- As an alert sender, I want the service to persist my alert before accepting it,
  so that a crash between acceptance and triage does not lose the incident.
- As an operator restarting the service, I want unfinished alerts recovered, so
  that work resumes without manual replay.
- As an operator, I want failed handling retained and successful handling removed,
  so that only unfinished work is recovered.

## RED evidence

```text
.venv/bin/pytest -q tests/test_queue.py
3 failed, 2 passed
```

The queue rejected the new `db_path` contract because it had no persistence
implementation. Restart recovery, success-only deletion, and duplicate pending
ID coalescing were therefore unavailable.

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Submission before worker startup survives and is processed after restart | `test_durable_queue_recovers_alert_submitted_before_worker_start` | restart integration | PASS |
| 2 | Failed handling retains the row for restart recovery | `test_durable_queue_removes_alert_only_after_success` | failure integration | PASS |
| 3 | Successful handling removes the persisted row | same target | integration | PASS |
| 4 | Duplicate pending alert IDs occupy one queue row | `test_durable_queue_coalesces_duplicate_pending_alert_ids` | persistence unit | PASS |
| 5 | Existing API, console, authentication, and demo flows still work | focused regression suite | integration | PASS |

Validation:

```text
.venv/bin/pytest -q tests/test_queue.py
5 passed

.venv/bin/pytest -q tests/test_queue.py tests/test_webhook.py \
  tests/test_webhook_auth.py tests/test_console.py tests/test_demo_mode.py
56 passed

.venv/bin/pytest -q
160 passed

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
160 passed
TOTAL 2091 statements, 248 missed, 88% coverage
```

## Known gaps

At the time of this slice, failed rows recovered only after restart. Persisted
timed retry and attempt counting shipped in
[durable queue slice 2](durable-alert-queue-slice-2.tdd.md). Processing leases,
dead-letter handling, maximum-attempt enforcement, and atomic cross-process job
claims remain open.

## Merge evidence

- RED checkpoint: `950b0a0 test: define durable alert queue recovery`
- GREEN checkpoint: `5a65617 feat: persist pending alerts in sqlite`
