# Shared Demo Alert TDD Evidence

## Source plan

The task comes from [`PLAN.md`](../../PLAN.md), Phase 4: Demo And Resolve
Actions.

## User journeys

- As a developer, I can change the local demo scenario in one place so the CLI,
  console, and tests do not drift.
- As a CLI user, I still get the deterministic `inc-demo-checkout-001` flow and
  output.
- As a console user, repeated demo triggers still receive collision-safe incident
  IDs and metrics.

## Task report

### RED

- Added unit tests for the deterministic scenario and console-specific overrides.
- Command:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest tests/test_demo.py -q`
- Result before implementation: `2 failed`.
- Intended failure: `ModuleNotFoundError: No module named 'incident_response.demo'`.
- Checkpoint: `e13b05b test: define shared demo alert contract`.

### GREEN

- Added `incident_response.demo.build_demo_alert()` as the typed source of truth.
- Added `build_unique_console_demo_alert()` for collision-safe browser demos.
- Replaced the CLI dictionary and console-local builder with the shared module.
- Re-ran the unit target: `2 passed`.
- Ran demo-focused CLI and console coverage: `16 passed, 31 deselected`.
- Checkpoint: `fd1f9a7 refactor: share CLI and console demo alert`.

### REFACTOR

- Updated the demo-mode integration fixture to use the same shared builder.
- Re-ran the demo-focused target: `16 passed, 31 deselected`.
- Checkpoint: `07922b0 refactor: reuse shared demo fixture in tests`.

## Test specification

| # | Guarantee | Test or command | Type | Result |
|---|---|---|---|---|
| 1 | The default builder returns the deterministic CLI scenario | `test_build_demo_alert_returns_the_deterministic_cli_scenario` | Unit | PASS |
| 2 | Console callers can override identity, time, metric, and source without duplicating common fields | `test_build_demo_alert_accepts_collision_safe_console_overrides` | Unit | PASS |
| 3 | The CLI completes alert, triage, fetch, resolve, and post-mortem generation | `test_demo_cli_runs_full_flow_without_anthropic_key` | Integration | PASS |
| 4 | Repeated console triggers produce different incident locations | `test_console_demo_alert_uses_collision_safe_incident_ids` | Integration | PASS |

## Validation

- Full suite and coverage:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest --cov=incident_response --cov-report=term-missing -q`
  → `149 passed`, `89%` total coverage, `100%` for `demo.py`.
- Lint and patch integrity:
  `.venv/bin/ruff check .` and `git diff --check`
  → passed.
- Offline smoke demo:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/incident-response demo ...`
  → accepted, triaged, fetched, resolved, and generated the post-mortem for
  `inc-demo-checkout-001`.

## Known warnings

The suite still emits the existing FastAPI TestClient deprecation warning.
Python 3.14 also reports existing SQLite resource warnings during coverage.
Neither warning fails the suite.
