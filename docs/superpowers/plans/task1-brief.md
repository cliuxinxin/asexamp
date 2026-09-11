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


## Task 1: Immutable commits and dependency records

Files: `backend/tcg/storage.py`, new `backend/tcg/operations.py`, new `backend/tcg/dependencies.py`, `tests/test_framework_storage_v270.py`.

Interfaces consumed by integration:

```python
# Existing Store.artifact / Store.revise_artifact signatures remain compatible.
# Add optional source_ids/source_roles/command_id/dependencies to revise_artifact.
Store.annotate_artifact(artifact_id, expected_revision, fields, reason='report_update', run_id=None)
dependencies.manifest(store, artifact_ids=(), source_ids=(), profile_ids=(), run_id=None) -> dict
dependencies.assert_manifest(store, value) -> None
dependencies.record_artifact(store, artifact) -> None
dependencies.artifact_status(store, artifact) -> dict
dependencies.impact(store, artifact_id, selected_ids=None) -> dict
```

- [ ] First demonstrate that report updates can rewrite a historical version, source or scope changes invalidate an input snapshot, and a downstream link can become stale. Use temporary real Store; two scenarios and their cases must make scope assertions meaningful.
- [ ] Centralize artifact creation/revision commit invariants in `operations.py` used by existing Store methods. Add atomic source enrichment parameters; return identical saved result for an already committed command. Refuse stale baselines and cancelled commands.
- [ ] Preserve source content/version digests and immutable profile snapshots needed in manifests. Artifact relation records retain exact version and item links derived from existing lineage; expose stale/needs_review/current without editing historical artifact data.
- [ ] Run focused tests and document APIs and limitations in `docs/superpowers/plans/task1-report.md`. Root will replace legacy report UPDATE call sites in Task 4.


Runtime: /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python; use PYTHONPATH=backend. Baseline real tests 16 passed. Write only owned files, no subagents, commit only owned files. Report named task1-report.md under docs/superpowers/plans.
