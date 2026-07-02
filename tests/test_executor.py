from incident_response.executor import (
    MockExecutor,
    RunbookStep,
    ShellExecutor,
    format_results_for_slack,
    parse_steps,
)
from incident_response.models import Runbook


def _runbook(body: str) -> Runbook:
    return Runbook(slug="test", title="Test", tags=[], content=body, path="test.md")


def test_parse_steps_reads_json_block():
    body = """
    ## Automated actions
    ```json
    [
      {"name": "rollback", "command": "deploy rollback checkout", "auto": true},
      {"name": "flip flag", "command": "feature-flag set x off"}
    ]
    ```
    """
    steps = parse_steps(_runbook(body))
    assert len(steps) == 2
    assert steps[0].name == "rollback"
    assert steps[0].auto is True
    assert steps[1].auto is False


def test_parse_steps_returns_empty_when_absent():
    assert parse_steps(_runbook("no actions here")) == []


def test_parse_steps_tolerates_bad_json():
    body = "## Automated actions\n```json\n{not json}\n```"
    assert parse_steps(_runbook(body)) == []


async def test_mock_executor_is_dry_run():
    steps = [RunbookStep(name="s", command="rm -rf /", auto=True)]
    results = await MockExecutor().run(steps)
    assert results[0].status == "dry_run"


async def test_shell_executor_skips_non_auto_steps():
    steps = [RunbookStep(name="s", command="echo hi", auto=False)]
    results = await ShellExecutor(allowed_prefixes=frozenset({"echo"})).run(steps)
    assert results[0].status == "skipped_not_auto"


async def test_shell_executor_blocks_commands_outside_allow_list():
    steps = [RunbookStep(name="s", command="rm -rf /", auto=True)]
    results = await ShellExecutor(allowed_prefixes=frozenset({"echo"})).run(steps)
    assert results[0].status == "skipped_not_allowed"


async def test_shell_executor_runs_allowed_auto_command():
    steps = [RunbookStep(name="hi", command="echo hello-world", auto=True)]
    results = await ShellExecutor(allowed_prefixes=frozenset({"echo"})).run(steps)
    assert results[0].status == "executed"
    assert "hello-world" in results[0].stdout


def test_format_results_for_slack_covers_all_statuses():
    steps = [
        RunbookStep(name="a", command="echo a", auto=True),
        RunbookStep(name="b", command="rm -rf /", auto=True),
    ]
    from incident_response.executor import StepResult
    results = [
        StepResult(step=steps[0], status="executed", stdout="a"),
        StepResult(step=steps[1], status="skipped_not_allowed", reason="not allowed"),
    ]
    text = format_results_for_slack(results)
    assert ":white_check_mark:" in text
    assert ":no_entry:" in text
