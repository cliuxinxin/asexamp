# Artifact capabilities verification — 2026-09-11

Implemented in `backend/tcg/conversation_artifacts.py` and `backend/tcg/artifact_actions.py`.

## Failure-first evidence

The focused suite uses the actual SQLite `Store`, actual schemas/Pydantic, and a controlled async model boundary. Before implementation, estimates and explanations failed because `_edit_token` was present while the model was answering; upstream synchronization produced only the analysis change, and an out-of-scope downstream edit was never validated. The missing adapter prevented the new capability behavior tests from running.

Later focused regressions were observed before their fixes: new evidence was absent from the paused Run; an unbounded project answer sent all 81 chunks; review report patches were rejected; safe-boundary/ancestor edits were rejected; historical reads rejected revision 1; a cancelled model response left an orphan proposal; the global confirmed-requirement cache remained stale after an analysis edit; effective template role overrides were ignored; ordinary edits could invent parent links; and replaying an applied proposal returned a later unrelated revision.

## Fresh verification

Commands run from release root:

```bash
PYTHONPATH=backend python -m unittest discover -s tests -p test_conversation_artifacts_v260.py -q
PYTHONPATH=backend python -m unittest discover -s tests -p test_workspace_actions_v2512.py -q
python -m py_compile backend/tcg/conversation_artifacts.py backend/tcg/artifact_actions.py
```

Results: 17 focused tests passed; 13 existing artifact action tests passed; both modules compiled. The checkout is an extracted release directory without Git metadata, so `git diff` is unavailable; this is unrelated to the successful test/compile commands.

Covered behaviors:

- Snapshot estimate/explanation finishes after the main Run progresses and the current artifact revision changes. No write lease, edit token, waiting guard, or proposal persistence occurs for these reads.
- Explicit historical saved versions remain readable; case reads expose actual steps and expectations.
- Explicit scoped edits save immediately, preserve manual execution data and unrelated rows, and leave workflow confirmation pending.
- Explicit preview/apply/discard remains version checked; cancellation and stale proposals cannot write.
- Analysis → scenarios → cases uses newly produced upstream drafts in one atomic apply; scoped lineage versions advance, unrelated rows remain identical, and invalid downstream operations save no partial upstream revisions.
- Readonly review creates no case revision; supported edits to saved review reports preserve lineage and execution values.
- New source IDs, roles, public/private input versions join the paused Run atomically with apply; existing stop conditions remain unchanged.
- Relevant source retrieval includes shared confirmed facts, caps evidence at 12 chunks / 24,000 text characters and explains omitted coverage.
- Supported safe boundaries and lineage ancestor/descendant edits retain the workflow gate.
- Late command cancellation rolls back proposal persistence; apply and preview receipts share the domain commit transaction. Applied proposal replays return the exact originally saved artifact snapshots.
- Ordinary edits cannot reparent selected cases/scenarios; additions require valid current parent IDs.
- Relevant upstream revisions refresh the exact Run's confirmed-requirement cache, ancestry revision and matching artifact snapshot; both input versions advance even without new source IDs.
- Learned template role overrides are applied before project evidence retrieval, excluding examples from business facts.
- Artifact analysis answers identify the exact source artifact revision in both metadata and visible text.

No real intranet model, HTTP server, or LangGraph execution is claimed by these focused tests. Root integration and lifecycle verification own those boundaries.
