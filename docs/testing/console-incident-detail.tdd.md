# Console incident detail TDD evidence

## Source and user journey

No source plan was provided. The journey was derived from the requested feature:
as an incident operator, I can open an incident from the console list and inspect
its alert, triage, remediation, and resolution context without switching to the
JSON API.

## Task report

- Added integration tests for a complete resolved incident, an in-progress
  incident, a missing incident, and untrusted stored content.
- RED command: `.venv/bin/pytest tests/test_console.py -q`
  - Result: `4 failed, 6 passed`; the new routes returned the expected pre-change
    `404`, and the missing route returned FastAPI's JSON response.
- GREEN command: `.venv/bin/pytest tests/test_console.py -q`
  - Result: `10 passed`.
- Full validation: `.venv/bin/pytest -q`
  - Result: `114 passed`.
- Lint: `.venv/bin/ruff check .`
  - Result: `All checks passed!`

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | A stored, triaged incident renders alert, suspect, impact, runbook, timeline, verification, and resolution data | `test_console_incident_detail_renders_triage_remediation_and_resolution` | Integration | PASS |
| 2 | An incident without triage renders a stable in-progress state | `test_console_incident_detail_handles_triage_in_progress` | Integration | PASS |
| 3 | An unknown incident returns a navigable HTML `404` | `test_console_incident_detail_returns_html_404` | Integration | PASS |
| 4 | Stored alert, tag, and timeline content is HTML-escaped | `test_console_incident_detail_escapes_untrusted_content` | Integration/security | PASS |

## Coverage and known gaps

`.venv/bin/pytest --cov=incident_response --cov-report=term-missing -q` passed
with 88% total coverage and 92% coverage for `console.py`. The console remains
local-first and unauthenticated. The demo-alert action is covered by
`console-demo-alert.tdd.md`, and console resolution is covered by
`console-incident-resolve.tdd.md`. The coverage run emits existing SQLite
`ResourceWarning` messages under Python 3.14; they do not fail the suite.

## Merge evidence

- RED checkpoint: `154d279 test: define incident console detail behavior`
- GREEN checkpoint: `cd3d889 feat: add incident console detail page`
