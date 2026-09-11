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


## Task 2: Shared context policies and capacity admission

Files: new `backend/tcg/context_service.py`, new `backend/tcg/context_budget.py`, `backend/tcg/direct.py`, `backend/tcg/model.py`, `tests/test_framework_context_v270.py`.

Interfaces consumed by integration:

```python
context_service.artifact_context(store, task, artifact, rows, evidence,
    instruction='', explicit_source_ids=(), extra=None) -> dict
context_service.rule_projection(report, rows=()) -> dict
context_service.analysis_signature(store, source_ids, roles, profile, scope=None) -> dict
context_budget.request_budget(directory, task, context, settings=None) -> dict
# request_budget returns fits/window/output_tokens/input_tokens/count_method/margin.
```

- [ ] Test that explaining one case includes its actual scenario/requirement ancestry and applicable rules, omits unrelated bulky report content, and estimates carry only selected scenarios plus design configuration. Test final request over capacity and configured-zero behavior.
- [ ] Construct immutable task ContextPacks with exact evidence IDs, consumed version manifest, selected/included/omitted coverage. Load recorded ancestor versions. Preserve mandatory referenced spans; retrieve optional material by task relevance; explicit new-source selection discloses incomplete coverage.
- [ ] Provide bounded global rule projections with IDs/provenance, not a repeated full report. Fix analysis signature to include semantic scope/rules/source content and role digests.
- [ ] Route DirectEngine.fits and final gateway admission through same serialization/count method. Default explicit conservative window 32768 and output allowance 8192; legacy 0 resolves to configured fallback, never unlimited. Add settings `context_window`, `output_tokens`, `output_limit_mode` (`request` default or `server` explicit) with validation. Server mode requires declared `server_output_tokens`; account for that enforced limit. Preserve custom gateway URL/auth protocol.
- [ ] Record final budget and model request digest through existing request recorder without exposing secrets. Run focused tests and report APIs in task2-report.md. Root will connect callers in workflow/flow/artifact_actions; do not edit those files.


Runtime: /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python; use PYTHONPATH=backend. Baseline real tests 16 passed. Write only owned files, no subagents, commit only owned files. Report named task2-report.md under docs/superpowers/plans.
