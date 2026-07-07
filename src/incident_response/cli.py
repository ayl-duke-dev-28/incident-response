"""Command-line entrypoint for local development and demos."""

from __future__ import annotations

import argparse
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Sequence, TextIO

from .config import Settings
from .main import create_app


_DEMO_ALERT = {
    "id": "demo-checkout-001",
    "title": "Checkout 5xx > 5%",
    "description": "checkout service error rate at 18%",
    "service": "checkout",
    "severity": "sev2",
    "triggered_at": "2026-07-02T21:05:00+00:00",
    "metric": "http.error_rate",
    "threshold": 0.05,
    "value": 0.184,
    "tags": {"env": "demo"},
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incident-response",
        description="Autonomous incident response server and offline demo.",
    )
    subcommands = parser.add_subparsers(dest="command")

    serve = subcommands.add_parser("serve", help="run the FastAPI server")
    serve.add_argument("--host", default="0.0.0.0", help="host to bind")
    serve.add_argument("--port", type=int, default=8080, help="port to bind")
    serve.add_argument("--reload", action="store_true", help="enable uvicorn reload")

    demo = subcommands.add_parser(
        "demo", help="run alert, triage, fetch, resolve, and postmortem offline"
    )
    demo.add_argument("--db-path", type=Path, default=Path("./demo-incidents.db"))
    demo.add_argument("--postmortem-dir", type=Path, default=Path("./demo-postmortems"))
    demo.add_argument("--runbooks-dir", type=Path, default=Path("./runbooks"))
    demo.add_argument("--webhook-token", default="demo-secret")
    demo.add_argument("--timeout-seconds", type=float, default=5.0)

    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "serve":
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port, reload=args.reload)
        return 0
    if command == "demo":
        return _run_demo(args, out)

    parser.error(f"unknown command: {command}")
    return 2


def _run_demo(args: argparse.Namespace, out: TextIO) -> int:
    settings = Settings(
        llm_mode="mock",
        github_mode="mock",
        slack_mode="mock",
        metrics_mode="mock",
        runbooks_dir=args.runbooks_dir,
        postmortem_dir=args.postmortem_dir,
        db_path=args.db_path,
        webhook_token=args.webhook_token,
        verification_enabled=False,
        log_level="CRITICAL",
    )
    app = create_app(settings=settings)
    headers = {"x-webhook-token": args.webhook_token}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    with TestClient(app) as client:
        accepted = client.post("/alerts", json=_DEMO_ALERT, headers=headers)
        if accepted.status_code != 202:
            out.write(f"failed alert submit {accepted.status_code}: {accepted.text}\n")
            return 1
        incident_id = accepted.json()["incident_id"]
        out.write(f"accepted {incident_id}\n")

        incident = _wait_for_triage(client, incident_id, args.timeout_seconds)
        if incident is None:
            out.write(f"failed triage timeout {incident_id}\n")
            return 1
        runbook = incident["triage"]["runbook"]["runbook"]["slug"]
        suspect = incident["triage"]["suspects"][0]["commit"]["sha"]
        out.write(f"triaged {runbook} suspect={suspect}\n")

        fetched = client.get(f"/incidents/{incident_id}")
        if fetched.status_code != 200:
            out.write(f"failed incident fetch {fetched.status_code}: {fetched.text}\n")
            return 1
        out.write(f"fetched {incident_id} status={fetched.json()['status']}\n")

        resolved = client.post(
            f"/alerts/{incident_id}/resolve",
            json={"resolution_note": "demo rollback complete"},
            headers=headers,
        )
        if resolved.status_code != 200:
            out.write(f"failed resolve {resolved.status_code}: {resolved.text}\n")
            return 1
        body = resolved.json()
        out.write(f"resolved {incident_id}\n")
        out.write(f"postmortem {body['postmortem_path']}\n")
        return 0


def _wait_for_triage(
    client: Any, incident_id: str, timeout_seconds: float
) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        fetched = client.get(f"/incidents/{incident_id}")
        if fetched.status_code == 200 and fetched.json().get("triage"):
            return fetched.json()
        time.sleep(0.05)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
