# Durable Alert Queue Slice 3 TDD Evidence

## Source

No separate source plan was provided. The journeys were derived for the third
small durable queue slice and recorded in [`PLAN.md`](../../PLAN.md).

## User journeys

- As an operator, I want permanently failing alerts to stop retrying after a
  configured limit, so that poison work cannot consume worker capacity forever.
- As an operator restarting the service, I want dead-letter records preserved
  and excluded from active queue depth, so that failures remain inspectable
  without being executed again.

## RED evidence

```text
.venv/bin/pytest -q tests/test_queue.py
2 failed, 10 passed
```

The failures proved that `AlertQueue` did not accept or validate a maximum
attempt count and could not move final failures out of the active queue.

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | A permanent failure is attempted exactly up to the configured limit | `test_durable_queue_dead_letters_after_maximum_attempts` | async integration | PASS |
| 2 | The final failure atomically leaves the active queue and retains its payload, attempt count, error, and timestamp | same target | SQLite integration | PASS |
| 3 | Dead letters survive queue reconstruction and do not count toward active depth | `test_dead_letters_survive_queue_restart_and_stay_out_of_depth` | restart integration | PASS |
| 4 | A non-positive maximum attempt count is rejected | `test_max_attempts_must_be_positive` | unit | PASS |

Validation:

```text
.venv/bin/pytest -q tests/test_queue.py
12 passed

.venv/bin/pytest -q
167 passed, 1 warning

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
167 passed, 1 warning
TOTAL 2130 statements, 250 missed, 88% coverage
```

## Coverage and known gaps

Coverage remains above the required 80% threshold. The warning is the existing
Starlette `TestClient` deprecation notice for its httpx compatibility layer.
Dead-letter listing and replay APIs are intentionally out of scope. Processing
leases and atomic cross-process job claiming remain future slices; deploy one
active queue worker per SQLite database until claiming ships.

## Merge evidence

- RED checkpoint: `c749450 test: define durable queue dead-letter behavior`
- GREEN checkpoint: `e6180f4 feat: dead-letter alerts after bounded retries`
