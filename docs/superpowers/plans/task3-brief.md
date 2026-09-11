# Versioned workflow framework implementation plan

> For agentic workers: use subagent-driven-development for bounded modules and focused review. The approved architecture is authoritative. Work continuously; the user has authorized implementation.

**Goal:** Implement the approved shared operation, immutable artifact, dependency, context and durable Turn contracts in a runnable v2.7.0 while preserving existing authoring behavior.

**Architecture:** The fixed LangGraph Run and durable conversation Turn share typed capability metadata, one artifact commit path, dependency manifests and task-specific context construction. Business writes remain version checked and idempotent; model calls remain outside transactions.

**Tech Stack:** Python, FastAPI, LangGraph, SQLite, React/TypeScript, existing model adapter.

**Spec:** `docs/TCG-WORKFLOW-FRAMEWORK-ARCHITECTURE.md` (user approved 2026-09-11).

## Global constraints

- Preserve existing working core stages, exported schemas, stable IDs, manual fields, project boundaries, template/sample distinction, proposal semantics and old thread recovery positions.
- A read/estimate must not mutate scenarios/cases or change a Run's stop policy. Draft adoption, confirmation and project sharing remain separate operations.
- Do not hold database transactions over model calls. Every committed revision is immutable. Domain writes must validate consumed versions and have retry-safe receipts.
- Runtime tests must use real LangGraph/FastAPI when dependencies can be obtained. Controlled model responses validate program behavior only. No unsupported claims of actual intranet model verification.
- Code comments English, user text Chinese. Keep tests focused on the complete workflow and concrete architecture invariants.
- Isolated local git branch `work/v2.7.0`, baseline `49c24b2`; old ZIP and data untouched. Parallel tasks have disjoint file ownership; root integrates shared files after contracts are available.


## Task 3: Capability-owned contracts and durable conversation Turn

Files: `backend/tcg/conversation.py`, `backend/tcg/conversation_context.py`, new `backend/tcg/turn_graph.py`, new `backend/tcg/capability_contracts.py`, `backend/tcg/conversation_artifacts.py`, `backend/tcg/conversation_project.py`, `backend/tcg/conversation_workflow.py`, `tests/test_framework_turn_v270.py`.

- [ ] Test snapshot estimate leaves Run state unchanged; resume after an already committed command does not rerun its mutation; unrelated yes after an explanation cannot approve old pending; two compatible actions execute in order.
- [ ] Capability definitions own target_types/target_required/effect/context_policy and optional prepare. Replace review optimize name special case with capability-owned preparation or explicit improvement capability while retaining legacy compatibility. Generic target resolver consumes metadata, not capability-name switches.
- [ ] Use a real persistent LangGraph Turn with stable turn thread ID and nodes for plan/one-command/result/optional-followup. Checkpoint only references/progress; operation receipts are authoritative for effects. Keep Controller as request adapter and scheduler; remove its duplicate recursive execution lifecycle. Waiting input/preview/deferred work can resume without repeating committed commands; retain cancel and safe-boundary handling. Use own AsyncSqliteSaver lifecycle lazily where necessary; close it safely.
- [ ] Central TURN_PROMPT states general contracts and relies on capability descriptions for business details. All shortened catalogues disclose totals/partial; provide bounded catalogue expansion capability for omitted targets. Commands and authoritative scope validation stay server-side.
- [ ] Workflow continue checks run/interrupt/artifact revision/control version. Preserve draft adoption then explicit submit, and distinguish turn-only negative constraints from durable Run control. Scoped ordinary affirmative replies cannot silently approve a historical pending.
- [ ] Existing adapters remain callable. For context construction, call `artifact_context` when available; coordinate exact hooks with root, who owns artifact_actions. Run focused real tests when environment available and record task3-report.md.


Runtime: /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python; use PYTHONPATH=backend. Baseline real tests 16 passed. Write only owned files, no subagents, commit only owned files. Report named task3-report.md under docs/superpowers/plans.
