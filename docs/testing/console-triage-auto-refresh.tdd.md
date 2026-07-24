# Console Triage Auto-Refresh TDD Evidence

## Source plan

The behavior follows the local-console roadmap in [`PLAN.md`](../../PLAN.md),
especially the risk around missing real-time feedback.

## User journeys

- As an on-call engineer, I can leave an incident detail page open while triage
  runs and see the completed evidence without manually reloading.
- As an operator, I can tell why the page is refreshing and how often.
- As an operator reviewing completed or resolved incidents, my page does not
  continue refreshing.

## Task report

### RED

- Added integration coverage for pending, completed, and resolved triage states.
- Command:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest tests/test_console.py -q -k 'auto_refresh'`
- Result before implementation: `1 failed, 2 passed, 40 deselected`.
- Checkpoint: `9b3bde4 test: define console triage auto-refresh behavior`.

### GREEN

- Added a conditional three-second HTML refresh to incident detail pages only
  while `incident.triage` is absent.
- Added a visible `role="status"` message explaining the active refresh.
- Re-ran the focused command.
- Result: `3 passed, 40 deselected`.
- Checkpoint: `96653fd feat: auto-refresh console during triage`.

## Test specification

| # | Guarantee | Test | Type | Result |
|---|---|---|---|---|
| 1 | Pending triage refreshes every three seconds and explains the behavior | `test_console_incident_detail_auto_refreshes_while_triage_is_in_progress` | Integration/accessibility | PASS |
| 2 | Completed triage does not refresh | `test_console_incident_detail_stops_auto_refresh_after_triage[inc-triage-complete-investigating]` | Integration | PASS |
| 3 | Resolved triage does not refresh | `test_console_incident_detail_stops_auto_refresh_after_triage[inc-resolved-static-resolved]` | Integration | PASS |

## Validation

- Console suite:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest tests/test_console.py -q`
  → `43 passed`.
- Full suite and coverage:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest --cov=incident_response --cov-report=term-missing -q`
  → `147 passed`, `89%` total coverage, `95%` for `console.py`.
- Lint and patch integrity:
  `.venv/bin/ruff check .` and `git diff --check`
  → passed.
- Standard browser QA:
  empty, demo, in-progress, completed, runbook, resolved, HTML `404`, and mobile
  states passed with no findings and a `100/100` health score.

## Browser evidence

The local QA report is stored at
`.gstack/qa-reports/2026-07-24-console-triage-auto-refresh/report.md`.
The browser observed one incident-page GET after the configured three-second
interval. After triage was made available, the next render removed the refresh
metadata and status banner, and a second four-second observation window contained
no network requests.

## Known warnings

The full suite still emits the existing FastAPI TestClient deprecation warning.
Python 3.14 also reports existing SQLite resource warnings during coverage.
Neither warning fails the suite.
