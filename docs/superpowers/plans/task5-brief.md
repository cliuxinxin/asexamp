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


## Task 5: Settings, observable result and continuous demo validation

Files: frontend model settings/types and existing result views as needed; `.env.example`, README, release metadata, new `demo/FRAMEWORK-DEMO.md`, integration tests.

- [ ] Expose context capacity and enforced output options in existing model settings; retain compact composer/wide workspace and review steps/expected result.
- [ ] Real HTTP + LangGraph continuous fixture: generate scenarios and pause → estimate without cases → explain → adopt/submit/share clarification → edit/sync preview → apply without continue → add source and impact → update/coverage → summary/export → continue from new snapshot. Verify partial result and version behavior directly in Store and Excel cells.
- [ ] Run only targeted affected suites, existing critical Auto/HITP and frontend checks; fix concrete regressions. One final independent spec/code review.
- [ ] Package clean v2.7.0 ZIP, persist deliverable and demo, provide explicit actual-model verification limit.


Runtime: /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python; use PYTHONPATH=backend. Baseline real tests 16 passed. Write only owned files, no subagents, commit only owned files. Report named task5-report.md under docs/superpowers/plans.
