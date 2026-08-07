# Durable Alert Queue Slice 5 TDD Evidence

## Source

No separate source plan was provided. The journeys were derived for the fifth
small durable queue slice and recorded in [`PLAN.md`](../../PLAN.md).

## User journeys

- As an operator, I want to replay one exhausted alert, so that corrected or
  transient failures can re-enter normal processing without editing SQLite.
- As an operator, I want replay to be atomic and conflict-safe, so that a failed
  request cannot lose the dead letter or overwrite active work.
- As an operator replaying within the same process, I want stale deduplication
  state cleared before the worker runs, so that the alert is actually processed.

## RED evidence

The queue and API contract failed before production code was added:

```text
.venv/bin/pytest -q tests/test_queue.py tests/test_dead_letters_api.py
4 failed, 17 passed, 1 warning
```

The failures showed that `AlertQueue` had no replay operation and FastAPI had no
replay route. A separate orchestrator test established the deduplication gap:

```text
.venv/bin/pytest -q \
  tests/test_orchestrator_dedup.py::test_prepare_replay_clears_the_alert_fingerprint
1 failed
```

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Replay atomically deletes the dead letter, creates a reset active row, prepares dedup state, and wakes the worker | `test_replay_dead_letter_resets_retry_state_and_wakes_worker` | async SQLite integration | PASS |
| 2 | Unauthenticated replay is rejected | `test_replay_dead_letter_requires_authentication` | API security | PASS |
| 3 | Replaying an unknown dead letter returns `404` | `test_replay_dead_letter_returns_not_found` | API integration | PASS |
| 4 | An existing active row returns `409` and both rows remain unchanged | `test_replay_dead_letter_conflict_preserves_both_rows` | transaction/conflict integration | PASS |
| 5 | Successful replay returns `202`, resets retry state, removes the dead letter, and prepares the orchestrator | `test_replay_dead_letter_returns_accepted_and_prepares_orchestrator` | API/SQLite integration | PASS |
| 6 | Replay removes the alert fingerprint from the in-memory dedup index | `test_prepare_replay_clears_the_alert_fingerprint` | unit | PASS |

Validation:

```text
.venv/bin/pytest -q tests/test_queue.py tests/test_dead_letters_api.py \
  tests/test_orchestrator_dedup.py
23 passed, 1 warning

.venv/bin/pytest -q
177 passed, 1 warning

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
177 passed, 1 warning
TOTAL 2187 statements, 251 missed, 89% coverage
```

## Coverage and known gaps

The warning is the existing Starlette `TestClient` deprecation notice for its
httpx compatibility layer. Replay is one alert at a time; bulk replay and console
controls are intentionally out of scope. Processing leases and atomic
cross-process job claiming remain future queue slices. Deploy one active queue
worker per SQLite database until claiming ships.

## Merge evidence

- RED checkpoints:
  - `e4fa711 test: define dead-letter replay API`
  - `0567754 test: require replay dedup reset`
- GREEN checkpoint: `f64dc6a feat: replay dead-lettered alerts`
