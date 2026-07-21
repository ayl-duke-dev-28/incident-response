# Console demo alert TDD evidence

## Source and user journey

No source plan was provided. The journey was derived from the requested feature:
as a local operator, I can trigger a safe checkout incident from an empty console
and land on its detail page without configuring credentials or calling the JSON
API manually.

## Task report

- Added `POST /console/demo-alert` using the existing background queue.
- RED command: `.venv/bin/pytest tests/test_console.py -q`
  - Result: `5 failed, 10 passed`; every new case reached the missing route or
    still rendered the unsafe button.
- GREEN command: `.venv/bin/pytest tests/test_console.py -q`
  - Result: `21 passed` after adding the happy path and security/error coverage.
- Full validation: `.venv/bin/pytest -q`
  - Result: `125 passed`.
- Lint: `.venv/bin/ruff check .`
  - Result: `All checks passed!`

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | A demo form submission queues a checkout incident and redirects to a working detail page | `test_console_demo_alert_enqueues_and_redirects_to_detail` | Integration | PASS |
| 2 | Repeated submissions receive distinct incident IDs and dedup fingerprints | `test_console_demo_alert_uses_collision_safe_incident_ids` | Integration | PASS |
| 3 | The button is hidden and POST is forbidden if any integration or remediation mode is not `mock` | `test_console_demo_alert_is_hidden_and_forbidden_outside_mock_mode` | Integration/security | PASS |
| 4 | Browser requests identified as cross-site by `Origin` or `Sec-Fetch-Site` are rejected before queue submission | `test_console_demo_alert_rejects_cross_site_browser_posts` | Integration/security | PASS |
| 5 | Queue exceptions return generic HTML without leaking exception details | `test_console_demo_alert_returns_safe_html_when_queue_submit_fails` | Integration/security | PASS |
| 6 | A slow worker returns a navigable accepted page instead of redirecting to a missing incident | `test_console_demo_alert_handles_slow_worker_without_broken_redirect` | Integration | PASS |

## Coverage and known gaps

`.venv/bin/pytest --cov=incident_response --cov-report=term-missing -q` passed
with 89% total coverage and 94% coverage for `console.py`. The console is still
local-first and unauthenticated. Mock-only mode prevents this endpoint from
calling real integrations or shell remediation, and cross-site browser POSTs are
rejected. Console resolution is covered by `console-incident-resolve.tdd.md`.
Existing SQLite `ResourceWarning` messages under Python 3.14 do not fail the
suite.

## Merge evidence

- RED checkpoint: `b268aa9 test: define console demo alert behavior`
- GREEN checkpoint: `f29e3b6 feat: wire mock-only console demo alerts`
