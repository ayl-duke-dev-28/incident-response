# Atomic Remediation Approval TDD Evidence

## Source

No separate source plan was provided. This follow-up closes the multi-instance
coordination limit documented by the initial
[remediation approval milestone](remediation-approval.tdd.md).

## User journeys

- As an operator running more than one service process, I want exactly one
  approval winner, so that one proposal cannot execute twice.
- As an incident commander, I want remediation completion to preserve concurrent
  incident changes, so that an execution result cannot revert a resolution or
  erase another process's timeline event.

## RED evidence

Command:

```text
.venv/bin/pytest -q tests/test_remediation_approval.py \
  -k 'two_orchestrators or concurrent_incident_updates'
```

Result before implementation:

```text
2 failed
```

Both orchestrators approved and executed the same pending proposal. The stale
completion write also changed a concurrently resolved incident back to
`investigating`.

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Two process-local orchestrators sharing SQLite produce one approval winner and one conflict | `test_two_orchestrators_cannot_approve_and_execute_the_same_proposal` | concurrency integration | PASS |
| 2 | Exactly one executor runs for a shared proposal | same target | concurrency integration | PASS |
| 3 | Completion preserves a concurrent resolution and timeline event | `test_execution_completion_preserves_concurrent_incident_updates` | stale-write regression | PASS |
| 4 | SQLite connections close after every transaction | full suite under Python 3.14 | resource lifecycle | PASS |

Validation:

```text
.venv/bin/pytest -q tests/test_db.py tests/test_remediation_approval.py \
  tests/test_orchestrator.py tests/test_orchestrator_features.py tests/test_console.py
62 passed

.venv/bin/pytest -q
157 passed

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
157 passed
TOTAL 2018 statements, 242 missed, 88% coverage
```

The coverage run emitted only the existing Starlette `TestClient` deprecation
warning. The earlier unclosed SQLite connection warnings are gone.

## Implementation guarantee

`IncidentStore.decide_remediation()` starts a SQLite `BEGIN IMMEDIATE`
transaction, reloads the latest incident, validates that remediation is still
pending, and writes the winner before releasing the database lock.
`finish_remediation()` performs the same reload-and-update pattern for execution
results, preserving unrelated fields and timeline entries.

## Merge evidence

- RED checkpoint: `1e3d824 test: reproduce cross-instance approval races`
- GREEN checkpoint: `7fbec3e fix: serialize remediation decisions in sqlite`
