"""Runbook remediation executor.

Runbooks may declare automated actions in a fenced JSON block titled
`## Automated actions`, e.g.:

    ## Automated actions
    ```json
    [
      {"name": "rollback checkout", "command": "deploy rollback checkout --confirm", "auto": true},
      {"name": "flip pricing cache off", "command": "feature-flag set checkout.pricing_cache off"}
    ]
    ```

Only steps with `"auto": true` AND matching an allow-listed command prefix ever run.
Everything else is emitted as a proposal that the human on-call approves manually.

The default `MockExecutor` never touches the system — it just records what would run.
Enable real execution by injecting `ShellExecutor` and configuring `ALLOWED_COMMANDS`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from .models import Runbook

logger = logging.getLogger(__name__)

_ACTIONS_BLOCK = re.compile(
    r"##\s*Automated actions\s*\n+```json\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass(frozen=True)
class RunbookStep:
    name: str
    command: str
    auto: bool = False


@dataclass(frozen=True)
class StepResult:
    step: RunbookStep
    status: str  # "executed", "skipped_not_auto", "skipped_not_allowed", "failed", "dry_run"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    reason: str = ""


def parse_steps(runbook: Runbook) -> list[RunbookStep]:
    match = _ACTIONS_BLOCK.search(runbook.content)
    if not match:
        return []
    try:
        entries = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        logger.warning(
            "runbook_actions_parse_failed",
            extra={"runbook": runbook.slug, "error": str(exc)},
        )
        return []
    steps: list[RunbookStep] = []
    for entry in entries:
        if not isinstance(entry, dict) or "command" not in entry:
            continue
        steps.append(
            RunbookStep(
                name=str(entry.get("name", entry["command"])),
                command=str(entry["command"]),
                auto=bool(entry.get("auto", False)),
            )
        )
    return steps


class RemediationExecutor(ABC):
    @abstractmethod
    async def run(self, steps: Iterable[RunbookStep]) -> list[StepResult]:
        ...


@dataclass
class MockExecutor(RemediationExecutor):
    """Never runs anything. Returns dry_run results — safe default for prod."""

    executed: list[StepResult] = field(default_factory=list)

    async def run(self, steps: Iterable[RunbookStep]) -> list[StepResult]:
        results = [
            StepResult(step=s, status="dry_run", reason="mock executor — no side effects")
            for s in steps
        ]
        self.executed.extend(results)
        return results


@dataclass
class ShellExecutor(RemediationExecutor):
    """Runs allow-listed commands via subprocess.

    A command is allowed if its first token (after shlex.split) is in `allowed_prefixes`.
    Non-`auto` steps are always skipped — real execution requires an explicit opt-in
    in the runbook file itself.
    """

    allowed_prefixes: frozenset[str]
    timeout_seconds: float = 30.0

    async def run(self, steps: Iterable[RunbookStep]) -> list[StepResult]:
        results: list[StepResult] = []
        for step in steps:
            if not step.auto:
                results.append(
                    StepResult(step=step, status="skipped_not_auto", reason="auto=false")
                )
                continue
            try:
                argv = shlex.split(step.command)
            except ValueError as exc:
                results.append(
                    StepResult(step=step, status="failed", reason=f"parse: {exc}")
                )
                continue
            if not argv or argv[0] not in self.allowed_prefixes:
                results.append(
                    StepResult(
                        step=step,
                        status="skipped_not_allowed",
                        reason=f"command '{argv[0] if argv else ''}' not in allow-list",
                    )
                )
                continue
            results.append(await self._exec(step, argv))
        return results

    async def _exec(self, step: RunbookStep, argv: list[str]) -> StepResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_seconds
            )
            return StepResult(
                step=step,
                status="executed" if proc.returncode == 0 else "failed",
                stdout=stdout.decode(errors="replace")[:4000],
                stderr=stderr.decode(errors="replace")[:4000],
                exit_code=proc.returncode,
            )
        except asyncio.TimeoutError:
            return StepResult(step=step, status="failed", reason="timeout")
        except FileNotFoundError as exc:
            return StepResult(step=step, status="failed", reason=str(exc))


def format_results_for_slack(results: list[StepResult]) -> str:
    if not results:
        return ""
    lines = ["*Automated remediation:*"]
    for r in results:
        icon = {
            "executed": ":white_check_mark:",
            "dry_run": ":memo:",
            "skipped_not_auto": ":ballot_box_with_check:",
            "skipped_not_allowed": ":no_entry:",
            "failed": ":x:",
        }.get(r.status, ":grey_question:")
        detail = r.reason or (r.stdout.splitlines()[0] if r.stdout else "")
        lines.append(f"  {icon} `{r.step.command}` — {r.status} {('— ' + detail) if detail else ''}")
    return "\n".join(lines)
