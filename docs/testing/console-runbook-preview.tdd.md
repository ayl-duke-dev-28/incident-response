# Console Runbook Preview TDD Evidence

## Source plan

The user journey and acceptance criteria came from
[`PLAN.md`](../../PLAN.md), Phase 3: Incident Detail.

## User journeys

- As an on-call engineer, I can open a matched runbook from an incident list or
  detail page so I can follow the response instructions without leaving the console.
- As an operator, I receive a navigable HTML `404` for an unknown runbook.
- As an administrator, I can expose configured runbooks without allowing a URL slug
  to read arbitrary files or inject HTML.

## Task report

### RED

- Added four integration tests covering links, preview content, unknown slugs, and
  traversal-shaped slugs.
- Command:
  `.venv/bin/pytest tests/test_console.py -q -k 'runbook_preview or links_matched_runbook'`
- Result before implementation: `4 failed, 36 deselected`.
- Checkpoint: `d5d8536 test: define console runbook preview behavior`.

### GREEN

- Added exact lookup over runbooks loaded during application startup.
- Added `GET /console/runbooks/{slug}`, links from incident list and detail pages,
  escaped Markdown rendering, responsive source styling, and HTML `404` handling.
- Re-ran the same focused command.
- Result: `4 passed, 36 deselected`.
- Checkpoint: `df4c549 feat: add safe console runbook previews`.

## Test specification

| # | Guarantee | Test | Type | Result |
|---|---|---|---|---|
| 1 | Matched runbooks link from both incident surfaces | `test_console_links_matched_runbook_from_list_and_incident_detail` | Integration | PASS |
| 2 | Preview shows title, tags, instructions, and automated actions without executing embedded HTML | `test_console_runbook_preview_renders_escaped_title_tags_and_instructions` | Integration/security | PASS |
| 3 | Unknown runbooks return a navigable HTML `404` | `test_console_runbook_preview_returns_navigable_html_404_for_unknown_slug` | Integration | PASS |
| 4 | Traversal-shaped URL input cannot read a file outside the loaded runbook library | `test_console_runbook_preview_does_not_resolve_url_slugs_as_filesystem_paths` | Integration/security | PASS |

## Coverage and known gaps

- Console suite:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest tests/test_console.py -q`
  → `40 passed`.
- Full suite and coverage:
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest --cov=incident_response --cov-report=term-missing -q`
  → `144 passed`, `89%` total coverage, `95%` for `console.py`.
- Lint:
  `.venv/bin/ruff check .`
  → `All checks passed!`
- The suite emits the existing FastAPI TestClient deprecation warning. Python 3.14
  also reports existing SQLite resource warnings during coverage; neither fails the
  suite.
- Browser visual QA remains a separate roadmap step.

## Security boundary

The route performs an exact match against immutable `Runbook` objects loaded at
startup. It never joins, opens, or resolves a filesystem path from URL input.
Runbook titles, tags, slugs, and Markdown content are HTML-escaped before rendering.

