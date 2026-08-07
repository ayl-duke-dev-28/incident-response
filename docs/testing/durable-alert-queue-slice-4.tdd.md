# Durable Alert Queue Slice 4 TDD Evidence

## Source

This is the fourth small durable queue slice recorded in
[`PLAN.md`](../../PLAN.md). It adds operator visibility without changing or
replaying stored dead letters.

## User journeys

- As an operator, I want to list exhausted alerts with their final failure
  context, so that I can diagnose poison work without opening SQLite directly.
- As an operator, I want dead-letter inspection authenticated and read-only, so
  that viewing failures neither exposes them publicly nor changes queue state.
- As an operator with many failures, I want newest-first bounded results, so that
  the most recent failures are visible without loading the entire table.

## API reference

`GET /dead-letters` accepts the same inbound authentication mechanisms as the
alert API. A valid `X-Webhook-Token` or one configured provider HMAC signature is
required.

| Query parameter | Type | Default | Constraint |
|---|---|---:|---|
| `limit` | integer | `50` | From `1` through `200`, inclusive |

Each response item contains:

| Field | Type | Meaning |
|---|---|---|
| `alert` | `Alert` | The original persisted alert payload. |
| `attempt_count` | integer | The number of failed handler attempts. |
| `last_error` | string | The final stored handler error, truncated to 500 characters when recorded. |
| `failed_at` | datetime | When the alert entered dead-letter storage. |

Results are ordered by `failed_at` descending, then alert ID ascending when
timestamps match. Listing does not delete or requeue any row. An application
using an in-memory-only `AlertQueue` has no durable dead letters and returns an
empty list from the queue accessor.

Example request:

```bash
curl http://localhost:8080/dead-letters \
  -H "x-webhook-token: change-me"
```

Example response:

```json
[
  {
    "alert": {
      "id": "ddg-9273",
      "title": "Checkout 5xx > 5%",
      "description": "checkout service error rate at 18%",
      "service": "checkout",
      "severity": "sev2",
      "triggered_at": "2026-08-06T12:00:00Z",
      "metric": "http.error_rate",
      "threshold": 0.05,
      "value": 0.184,
      "tags": {},
      "raw": {}
    },
    "attempt_count": 5,
    "last_error": "upstream unavailable",
    "failed_at": "2026-08-06T12:01:00"
  }
]
```

## RED evidence

The tests were run against checkpoint `49834c4`, before the endpoint and queue
accessor existed:

```text
PYTHONPATH=<49834c4-tree>/src .venv/bin/pytest -q \
  <49834c4-tree>/tests/test_dead_letters_api.py
4 failed, 1 warning
```

All requests returned `404`, proving that authentication, listing, limiting,
and response serialization were not implemented.

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Unauthenticated listing is rejected | `test_list_dead_letters_requires_authentication` | API security | PASS |
| 2 | Results are newest first and include the original alert plus failure context | `test_list_dead_letters_returns_newest_first_with_failure_context` | API/SQLite integration | PASS |
| 3 | `limit` bounds the response without deleting stored rows | `test_list_dead_letters_applies_limit_without_deleting_rows` | read-only integration | PASS |
| 4 | Limits below 1 or above 200 return `422` | `test_list_dead_letters_rejects_out_of_range_limit` | API validation | PASS |

Validation:

```text
.venv/bin/pytest -q tests/test_dead_letters_api.py
4 passed, 1 warning

.venv/bin/pytest -q
171 passed, 1 warning

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term -q
171 passed, 1 warning
TOTAL 2148 statements, 251 missed, 88% coverage
```

## Coverage and known gaps

The warning is the existing Starlette `TestClient` deprecation notice for its
httpx compatibility layer. Listing is intentionally read-only. Authenticated
replay, processing leases, and atomic cross-process job claiming remain future
slices. Deploy one active queue worker per SQLite database until claiming ships.

## Merge evidence

- RED checkpoint: `49834c4 test: define dead-letter listing API`
- GREEN checkpoint: `986ae5d update`
