# Autonomous Incident Response

An AI system that responds to production outages the moment an alert fires. It:

1. Receives the alert on a webhook.
2. Runs **three agents in parallel** using Claude:
   - **Triage** — ranks recent commits by likelihood of being the culprit (timing, files, message, size).
   - **Runbook** — matches the alert to your on-disk runbook library.
   - **Impact** — estimates affected users from error-rate + traffic + active-user counts.
3. Composes a concise Slack brief and posts it to your incident channel.
4. On resolve, generates a **blameless post-mortem** markdown, saves it to `./postmortems/`, and threads a link back to the original Slack message.

## Architecture

```
alert webhook ─► FastAPI (/alerts)
                     │
                     ▼
              Orchestrator
              │  (asyncio.gather)
     ┌────────┼──────────┬──────────────┐
     ▼        ▼          ▼              ▼
  GitHub   Metrics    Runbooks       Anthropic
 (recent  (error/    (frontmatter    (JSON-mode
  commits) rps/users)  markdown)      agents)
     │        │          │              │
     └────────┴──────────┴──────────────┘
                     │
                     ▼
              Slack brief posted
              SQLite state saved

on resolve:
   /alerts/{id}/resolve ─► post-mortem markdown ─► Slack thread reply
```

Every external system has a `Mock*` implementation and a real one behind the same interface, so the whole flow works offline and in tests.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # set ANTHROPIC_API_KEY at minimum
uvicorn incident_response.main:create_app --factory --reload --port 8080
```

Fire a test alert:

```bash
curl -X POST http://localhost:8080/alerts \
  -H "x-webhook-token: change-me" \
  -H "content-type: application/json" \
  -d '{
    "id": "ddg-9273",
    "title": "Checkout 5xx > 5%",
    "description": "checkout error rate at 18%",
    "service": "checkout",
    "severity": "sev2",
    "triggered_at": "2026-07-02T21:05:00+00:00",
    "metric": "http.error_rate",
    "threshold": 0.05,
    "value": 0.184,
    "tags": {"env": "prod"}
  }'
```

The response contains the full triage report. Copy the `id` (e.g. `inc-ddg-9273`) and resolve it:

```bash
curl -X POST http://localhost:8080/alerts/inc-ddg-9273/resolve \
  -H "x-webhook-token: change-me" \
  -H "content-type: application/json" \
  -d '{"resolution_note": "rolled back a1b2c3d"}'
```

The post-mortem lands at `./postmortems/YYYY-MM-DD-inc-ddg-9273.md`.

## Wiring to real systems

Flip the mode env vars in `.env`:

| Env var | Modes | Notes |
|---|---|---|
| `GITHUB_MODE` | `mock`, `rest` | `rest` needs `GITHUB_TOKEN` + `GITHUB_REPO` |
| `SLACK_MODE` | `mock`, `webhook` | `webhook` needs `SLACK_WEBHOOK_URL` |
| `METRICS_MODE` | `mock`, `datadog` | `datadog` needs `DATADOG_API_KEY` + `DATADOG_APP_KEY` |

Adding another provider (PagerDuty, GitLab, Prometheus) is a matter of implementing the abstract client in `integrations/` and registering it in the `build_*_client` factory.

## Runbook library

Drop markdown files in `./runbooks/`. Frontmatter:

```markdown
---
title: My scenario
tags: [service, symptom]
---

## First actions
1. ...
```

The runbook agent sees titles, tags, and the first body line — enough to rank without shipping the whole library to the LLM every time.

## Tests

```bash
pytest
```

The suite uses `FakeLLM` and mock adapters — no network required.

## Layout

```
src/incident_response/
  main.py              FastAPI app + webhook endpoints
  orchestrator.py      Alert → triage → brief → resolve → post-mortem
  models.py            Immutable pydantic domain models
  db.py                SQLite persistence
  config.py            Env-driven settings
  runbooks_loader.py   Markdown frontmatter parser
  agents/
    llm.py             Anthropic wrapper + FakeLLM
    triage.py          Suspect-commit ranking
    runbook.py         Runbook selection
    impact.py          User-impact estimate
    brief.py           Deterministic Slack brief composer
    postmortem.py      LLM post-mortem generator
  integrations/
    github.py          Recent commits (mock + REST)
    slack.py           Post + thread (mock + webhook)
    metrics.py         Error rate / rps / active users (mock + Datadog)
tests/                 pytest suite (no network)
runbooks/              Example markdown runbooks
```
# incident-response
# incident-response
