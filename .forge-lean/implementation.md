### Approach
Delegation is a derived gate mode: `ProductionGraph.gate_for` turns a HUMAN gate into `GateMode.AUTO_DELEGATED` when the persisted `AutonomyPolicy` names the node or its kind. When a delegated output clears review, `_settle` asks `AutonomyPolicy.escalations` whether it can pass. It passes only if the panel ran, has no confirmed HIGH, has an overall score ≥ min_score, and (for edit/delivery) conformance ran and passed. A passing gate becomes CLEAN with approver=delegate and its evidence. Otherwise it waits for the owner, with the reasons in `escalated`. Budget holds that stay within the policy ceiling for every exceeded metric build anyway and are recorded as `BudgetApproval(approver="auto")`.

### Changes
- src/spectracut/studio/autonomy.py: new. AutonomyPolicy, BudgetCeiling, BudgetApproval, load_autonomy.
- src/spectracut/studio/gates.py: GateMode.AUTO_DELEGATED; GateDecision.approver/evidence/escalated (all defaulted).
- src/spectracut/studio/graph.py: `autonomy` attribute, persisted in GraphSnapshot. gate_for returns the delegated mode. `_conform` now also returns the conformance outcome. `_gate_decision` records approver and evidence. Owner approve sets approver=owner.
- src/spectracut/studio/producer.py: Producer.run takes `autonomy`/`conformance`. hold() auto-approves within the ceiling and fills `ProducerReport.auto_approved`.
- src/spectracut/studio/store.py: ProductionMeta.autonomy and budget_log.
- src/spectracut/cli/studio.py: `--autonomy`/`--delegate` on new and run. run logs auto-approved budget. approve-budget writes to budget_log. status appends by=/evidence=/escalated= and adds a "budget approvals:" section.
- src/spectracut/studio/facts.py: wrapped one line that was already over-long on the base, so V5's ruff check passes (amendment add-deliverable).
- tests/studio/test_autonomy.py: 15 tests covering contract cases (a)–(i).

### Acceptance Criteria Status
- R1 ✅ src/spectracut/studio/graph.py:399 (gate_for), src/spectracut/studio/autonomy.py:53; test_autonomy.py:78
- R2 ✅ src/spectracut/studio/autonomy.py:68 (escalations), src/spectracut/studio/graph.py:807 (_gate_decision); test_autonomy.py:108-181
- R3 ✅ src/spectracut/studio/producer.py:355, src/spectracut/cli/studio.py:399; test_autonomy.py:224,238,342
- R4 ✅ src/spectracut/cli/studio.py:444 (_approval), :433 (budget approvals); test_autonomy.py:315,331
- R5 ✅ V2–V4 pass

### Verification
- V1 `uv run pytest -q tests/studio/test_autonomy.py`: 15 passed. Without the fix it fails, because `spectracut.studio.autonomy` cannot be imported.
- V2 studio graph/producer/contract/facts/supervisor/intake/department: 87 passed
- V3 cli + critique: 69 passed
- V4 acceptance + layout + spdx: 73 passed, 5 skipped
- V5 mypy: no issues. ruff: all checks pass after the facts.py wrap.
- `forgedock check`: PASS

### Notes
- With no policy, behaviour is unchanged.
- With a policy, `EstimateReport.human_gates` no longer counts delegated gates, since gate_for resolves them to AUTO_DELEGATED.
- `studio run` does not wire a critic yet (K6). From the CLI, delegated gates therefore escalate with "no panel review".
- No new env vars.

## CI follow-up
Stale-list check flagged autonomy -> tests/studio/test_producer.py:46. That line is the _brief() fixture, not an enumeration of studio modules; recorded in result.json not_members.
