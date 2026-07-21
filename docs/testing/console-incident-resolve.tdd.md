# Console incident resolve TDD evidence

## Source and user journey

No source plan was provided. The journey was derived from the requested next
console update: as a local operator, I can record how a triaged incident was
resolved, generate its post-mortem, and remain in the HTML console.

## Task report

- Added a resolve form to triaged, unresolved incident detail pages and
  `POST /console/incidents/{id}/resolve`.
- Initial RED command: `.venv/bin/pytest tests/test_console.py -q`
  - Result: `13 failed, 21 passed`; the form and route did not exist.
- Initial GREEN command: `.venv/bin/pytest tests/test_console.py -q`
  - Result: `35 passed`.
- Unicode boundary RED command:
  `.venv/bin/pytest tests/test_console.py::test_console_resolve_accepts_500_unicode_characters -q`
  - Result: `1 failed`; a valid 500-character emoji note exceeded the raw body cap.
- Final GREEN command: `.venv/bin/pytest tests/test_console.py -q`
  - Result: `36 passed`.
- Full validation: `.venv/bin/pytest -q`
  - Result: `140 passed`.
- Lint: `.venv/bin/ruff check .`
  - Result: `All checks passed!`

## Test specification

| # | What is guaranteed | Test | Type | Result |
|---|---|---|---|---|
| 1 | The form appears only for triaged, unresolved incidents in all-mock mode | `test_console_incident_detail_shows_resolve_form_only_for_open_mock_incidents` | Integration/security | PASS |
| 2 | Resolution persists status and note, generates a post-mortem, and redirects with `303` | `test_console_resolve_updates_incident_generates_postmortem_and_redirects` | Integration | PASS |
| 3 | Missing, already resolved, and still-triaging incidents return HTML `404` or `409` responses without invoking resolution | `test_console_resolve_returns_html_404_for_unknown_incident`, `test_console_resolve_rejects_duplicate_resolution`, `test_console_resolve_waits_for_triage_to_finish` | Integration | PASS |
| 4 | Only form-encoded notes of at most 500 characters are accepted, including 500 non-ASCII characters | `test_console_resolve_validates_content_type_and_note_length`, `test_console_resolve_accepts_500_unicode_characters` | Integration/security | PASS |
| 5 | Cross-site browser POSTs and every non-mock integration/remediation mode are rejected | `test_console_resolve_rejects_cross_site_browser_posts`, `test_console_resolve_is_hidden_and_forbidden_outside_mock_mode` | Integration/security | PASS |
| 6 | Internal resolution failures return generic HTML without leaking exception details | `test_console_resolve_returns_safe_html_when_resolution_fails` | Integration/security | PASS |

## Coverage and known gaps

`.venv/bin/pytest --cov=incident_response --cov-report=term-missing -q` passed
with 89% total coverage and 93% coverage for `console.py`. The console remains
local-first and unauthenticated, so its write routes stay all-mock-only and reject
cross-site browser submissions. Real integration modes continue to use the
authenticated JSON resolve API. Existing Python 3.14 SQLite `ResourceWarning`
messages and the TestClient deprecation warning do not fail the suite.

## Merge evidence

- Initial RED checkpoint: `4812655 test: define console incident resolve behavior`
- Initial GREEN checkpoint: `60ace93 feat: add mock-only console resolve action`
- Unicode RED checkpoint: `8df1880 test: cover unicode resolution note boundary`
- Unicode GREEN checkpoint: `24e2d54 fix: accept unicode resolution notes at limit`
