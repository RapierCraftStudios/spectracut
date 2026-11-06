**Verdict**: CONFIRMED
**Confidence**: high
**Severity**: medium (P0 milestone feature; every human gate and budget hold stops an unattended run)
**Task Type**: feature

### What Was Claimed
Every human gate stops the run until `studio approve`, and budget holds stop until `studio approve-budget`, even when the critique is strong and the spend is small. The issue asks for an AutonomyPolicy that delegates chosen gates, a `GateMode.AUTO_DELEGATED` that passes on panel (no confirmed HIGH) + conformance pass + score threshold or escalates to the owner with reasons, budget holds that auto-approve under a policy spend ceiling, a `--autonomy` CLI option, and `studio status` showing who approved each gate and on what evidence.

### What We Found
Verified against `origin/milestone/m2-first-film-alterlab-launch-film` @ 2f2d428 (HEAD = base tip).
- `src/spectracut/studio/gates.py:30-33`: `GateMode` has only `AUTO`, `CRITIC`, `HUMAN`; no delegated mode.
- `src/spectracut/studio/gates.py:75-81`: `GateDecision` records `mode`, `critic`, `human` (approved/rejected) and `comment`; there is no approver (owner/delegate/auto) and no evidence field.
- `src/spectracut/studio/gates.py:84-98`: `GatePolicy.resolve` is fixed: node override, then production override, then profile default. No policy can delegate a gate.
- `src/spectracut/studio/graph.py:773-777`: in `_settle`, any `HUMAN` gate that passes review goes to `AWAITING_APPROVAL` unconditionally, even with a clean panel and a passed conformance.
- `src/spectracut/studio/graph.py:706-725`: `_conform` runs only for `CONFORMANCE_KINDS = {"edit","delivery"}` (`conformance.py:28`) and folds a non-passed review into a confirmed HIGH finding. Whether conformance ran and passed is not recorded anywhere `_settle` can read.
- `src/spectracut/studio/graph.py:420-432,460-462`: `approve()` and `_decide` write `human="approved"` with no actor.
- `src/spectracut/studio/producer.py:324-342`: `hold()` holds any node whose projected spend exceeds a cap unless its id is in `approved`; there is no ceiling for auto-approval. `ProducerReport` (`producer.py:118-124`) has no record of auto-approvals.
- `src/spectracut/studio/store.py:63-72`: `ProductionMeta` keeps `caps` and `budget_approvals` (bare node ids). It records no approver, no evidence and no autonomy policy.
- `src/spectracut/cli/studio.py:322-359`: `studio run` passes `caps` and `approved` only. `studio status` (`cli/studio.py:362-384`) prints `human=` and `critic=` and nothing about who approved or why. `approve-budget` (`cli/studio.py:423-440`) appends a bare id.
- `src/spectracut/critique/panel.py:126-140`: `ReviewerPanel` returns `passed = not blocked` with `scores={"overall": 0.0|1.0}` (`critic.py:54` `OVERALL="overall"`). `StudioCritic` passes when `overall >= pass_score` (`critic.py:373`). This is the "score" a delegated gate can threshold on.
- `src/spectracut/studio/estimate.py:297`: `human_gates` counts only `GateMode.HUMAN`. A delegated gate would no longer count as a human gate, which is correct.
- `studio run` passes no critic and no conformance to the graph (`cli/studio.py:340-348`). Under the CLI, a delegated gate therefore has no panel evidence and must escalate. This is the safe default.

### Root Cause
Gate modes are fixed per profile and production (`gates.py:84-98`, `profiles.py:107-114`). `_settle` treats every HUMAN gate as needing a person (`graph.py:773`). Budget approval is a manual allow-list (`producer.py:333`). No policy object can delegate a gate or approve spend, and no decision records who approved or on what evidence.

### Affected Files
- `src/spectracut/studio/autonomy.py` (new): `AutonomyPolicy` with delegated gate kinds and/or node ids, `min_score`, per-metric `budget_ceiling`, `qualifies(spec, verdict, conformance_ran, conformance_needed) -> list[str]` escalation reasons, and `BudgetApproval` record.
- `src/spectracut/studio/gates.py`: `GateMode.AUTO_DELEGATED`; `GateDecision.approver: Literal["owner","delegate","auto"] | None`, `evidence: list[str]`, `escalated: list[str]`.
- `src/spectracut/studio/graph.py`: `ProductionGraph.autonomy` (persisted on `GraphSnapshot.autonomy`, default None); `gate_for` maps HUMAN→AUTO_DELEGATED for delegated gates; `_build_at` tracks whether conformance ran and passed; `_settle` auto-accepts a qualifying AUTO_DELEGATED node (approver="delegate", evidence) or parks it in AWAITING_APPROVAL with `escalated` reasons; `approve()` records approver="owner"; AUTO/CRITIC accepts record approver="auto". A delegated `brief` node still freezes the contract.
- `src/spectracut/studio/producer.py`: `run(..., autonomy=None)`; an over-cap node whose projected spend is within the policy ceiling builds and is listed in `ProducerReport.auto_approved: list[BudgetApproval]`; above the ceiling it is held as today.
- `src/spectracut/studio/store.py`: `ProductionMeta.autonomy: AutonomyPolicy | None = None` and `budget_log: tuple[BudgetApproval, ...] = ()` (both defaulted, so old production.json loads).
- `src/spectracut/cli/studio.py`: `--autonomy PATH` (JSON policy) and repeatable `--delegate GATE` on `new` and `run`; `run` passes the policy to the graph and Producer and appends auto budget approvals to `budget_log`; `approve-budget` logs approver=owner; `status` appends ` by=<owner|delegate|auto>` and ` evidence=...` / ` escalated=...` to each gate line, plus a budget-approvals section.
- `tests/studio/test_autonomy.py` (new).
- Enumerating tests named by the engine (verified, none need edits):
  - `tests/studio/test_producer.py:46` is the `Recorder` fake-runner class, not a list of studio modules. PR #259 recorded `conformance`/`contract` as non-members for the same reason (commit e0de2a9). `autonomy.py` is likewise a non-member.
  - `tests/cli/test_cli.py:92` asserts per-stage `department=` lines. `tests/cli/test_cli.py:2` is the module docstring. Neither lists exports or files that a new option would have to join. No new CLI module or export is added.
  - Still covered by V2 and V3 so CI stays green.

### Sibling Sweep
- `graph.py:461`: `_decide` defaults the mode to HUMAN. The fix must keep AUTO_DELEGATED when the owner approves an escalated delegated node.
- `estimate.py:265,297`: AUTO_DELEGATED is not AUTO, so revision runs are still estimated. It is not HUMAN, so it is not counted as a human gate. Correct as is; no change.
- `producer.py:185` and `producer.py:282-289` (`add_step` gate override): these pass GateMode through and accept the new member without change.
- Device-shot path `cli/studio.py:337-338` calls `graph.run` directly with no Producer. The delegated gate logic lives in the graph, so it applies there too. Budget auto-approval does not apply because that path has no caps today.
- No other approval bypasses were found (`grep -rn "GateMode\." src`).

### History
- The gate model was introduced in ef79c88 (#43, production graph engine). Budget caps and approvals came from the Producer work, persisted in `ProductionMeta` (#43 lineage).
- The confirmed-HIGH-only panel came from 8b2d531 (#243). Contract and conformance came from PR #259 (#242), merged as 2f2d428. Facts/decision records came from 0d8c8fc (#245).
- No prior attempt at autonomy or auto-gates was found; this is new work.

### Recommendation
Implement in one lane (about 7 files, roughly 450 lines including tests). Delegation is opt-in. With no policy, every gate and hold behaves exactly as today. Escalation is the default whenever evidence is missing: no critic verdict, a missing `overall` score, a score below `min_score`, any confirmed HIGH, or (for edit/delivery) no contract, no conformance callable, or a conformance that did not pass. `AutonomyPolicy` is persisted on the graph snapshot so `studio status` and later runs see it.

### Related Issues
- #242 (contract/conformance, closed), #243 (panel, closed): dependencies, both merged.
- #244 (supervisor), #245 (facts): context.
- #247 `spectracut make` autopilot (open): the downstream consumer of this policy.
- #252/#253 (edit/delivery departments, honest e2e; open): conformance at edit and delivery only becomes real once they land.
