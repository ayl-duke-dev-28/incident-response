# Remediation Approval TDD Evidence

## Source

No separate source plan was provided. The journey was derived from the requested
next update and recorded in [`PLAN.md`](../../PLAN.md) as the human remediation
approval milestone.

## User journeys

- As an on-call operator, I want matched runbook actions to wait for my decision,
  so that incident triage cannot execute remediation autonomously.
- As an approver, I want my identity, note, and exact reviewed commands persisted,
  so that the audit trail explains what I authorized.
- As an incident commander, I want to reject a proposal permanently, so that a
  later retry cannot execute an unsafe action.
- As an API or local-console operator, I want explicit approve and reject
  controls, so that I can decide remediation from the surface I already use.

## RED evidence

Command:

```text
.venv/bin/pytest -q tests/test_remediation_approval.py
```

Initial result:

```text
5 failed
```

The failures proved that the executor ran during `handle_alert`, approval and
rejection methods did not exist, the authenticated API returned `404`, and the
console had no approval controls.

A later safety review added a runbook-mutation regression:

```text
.venv/bin/pytest -q tests/test_remediation_approval.py::test_approval_executes_the_persisted_proposal_if_runbook_changes
1 failed
```

The failure showed that the first implementation re-read the runbook after
approval instead of executing the stored command snapshot.

## GREEN evidence

| # | Guarantee | Test target | Type | Result |
|---|---|---|---|---|
| 1 | Triage persists a pending proposal and does not invoke the executor | `test_runbook_remediation_waits_for_persisted_approval` | integration | PASS |
| 2 | Approval persists operator identity and executes at most once | `test_approval_executes_once_and_persists_operator_decision` | integration | PASS |
| 3 | Rejection is final and never invokes the executor | `test_rejection_is_final_and_never_executes` | integration | PASS |
| 4 | Approval executes the exact reviewed snapshot after a runbook edit | `test_approval_executes_the_persisted_proposal_if_runbook_changes` | regression | PASS |
| 5 | The approval API requires webhook authentication | `test_authenticated_api_can_approve_pending_remediation` | API integration | PASS |
| 6 | The local console renders and submits approval controls | `test_console_renders_and_submits_pending_approval_actions` | UI integration | PASS |
| 7 | Verification begins only after approved real execution | `test_verification_scheduled_after_real_execution` | integration | PASS |

Focused GREEN:

```text
.venv/bin/pytest -q tests/test_remediation_approval.py tests/test_orchestrator.py tests/test_orchestrator_features.py
13 passed
```

Full validation:

```text
.venv/bin/pytest -q
155 passed

.venv/bin/ruff check .
All checks passed!

.venv/bin/pytest --cov=incident_response --cov-report=term-missing -q
155 passed
TOTAL 1993 statements, 242 missed, 88% coverage
```

## Coverage and known gaps

Coverage remains above the required 80% threshold. At the time of this feature,
the approval lock was process-local. That gap was closed by the follow-up
[atomic approval evidence](remediation-approval-atomicity.tdd.md).

## Merge evidence

- RED checkpoint: `2ac16d9 test: define remediation approval gate`
- GREEN checkpoint: `6bed23c update`
- Final guarantee: no matched runbook action reaches an executor before one
  persisted operator approval, and approval runs the stored proposal only.
