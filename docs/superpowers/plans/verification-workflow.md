# Workflow and clarification verification — 2026-09-11

Implemented `clarification.py`, `conversation_workflow.py`, and safe-boundary hooks in the existing `graph.py` / `workflow.py`. No second workflow engine or new database tables were introduced.

The first tests were authored in `tests/test_conversation_workflow_v260.py` before production implementation. Their initial invocation could not collect: both the default interpreter and the primary runtime lack pytest; the runtime agent also confirmed FastAPI/LangGraph dependencies unavailable. This is an environment failure, not a demonstrated behavioral red-to-green result. Those full WorkflowEngine integration tests remain present for a provisioned environment.

Executed against the real SQLite Store with the primary runtime:

```bash
PYTHONPATH=backend "$CODEX_PRIMARY_RUNTIME_PYTHON" -m unittest discover -s tests -p test_conversation_clarification_v260.py -v
PYTHONPATH=backend "$CODEX_PRIMARY_RUNTIME_PYTHON" -m unittest discover -s tests -p test_conversation_boundary_v260.py -v
```

Results: 6 draft/control tests and 4 boundary tests passed. Assertions cover shared draft adoption, card edits, database close/reopen recovery, independent submission/sharing, one source on repeat save, source provenance and supersession, stale revision/question-set rejection, full-textarea reconciliation without retaining contradictory old answers, clearing adopted answers, persistent stop goals, cancellation of older continue versions, current-stage preservation before pause, correct reasons for successive boundaries, and rejection of late cancelled/stale-input model results.

Boundary tests execute production Engine method bodies extracted through Python AST, use real Store/Diagnostics, and use event-controlled model responses. They do **not** verify LangGraph checkpoint scheduling or FastAPI routing. Full-engine tests cover those behaviors but could not run in this environment.

Peer review caught an interrupt-index hazard in an initial wrapper design. The implementation now uses fixed `boundary_*` graph nodes before each original v7 business node. Each guard owns a single index-zero interrupt, so clarification/strategy/scenario nodes retain their original interrupt order. Existing business node names remain registered for v7 checkpoint compatibility. Safe-boundary tests exercise the actual guard method body; real LangGraph migration/replay remains an unavailable integration gate.

`python -m compileall -q` passed for the four production modules. Existing project clarification apply/share regression passed. Running the whole `test_project_context_v2512.py` file reported one unrelated existing token-key mismatch in its supplement rollback test (test sets project ID while `Store.create_run` checks chat ID); sent to the project owner.

Integration contract:

- `conversation_workflow.execute(store, engine, chat, name, args, turn_id=None)`; `turn_id` is the per-command receipt ID. Writes commit their receipt inside the same Store transaction.
- `register_routes(app, store=None, engine=None)` exposes GET/PATCH `/api/runs/{run_id}/clarification-draft`, returning the draft directly. `answers` is an object mapping stable question IDs to strings.
- `engine.request_boundary(run_id, reason='write', command_id=None)` persists a request; current valid stage results may finish. `engine.on_safe_boundary(run_id)` runs after a real waiting interrupt is persisted. Pending scope updates apply before this callback.
- Only `workflow_paused` reasons `write`/`scope`, with no explicit hold or unresolved write, can be automatically resumed by the controller. Reasons `pause`/`stop_after` require explicit user control.
- Existing `Engine.schedule` completion callback reschedules a run changed to `queued` while its prior runner is still registered. Recovery must also drain and then inspect already-waiting boundary runs.
- `input_version` rejects outdated model output; `control_version` and controller continuation epoch reject superseded continuation. A later pause preserves committed edits and invalidates pending continuation.

No real intranet model execution is claimed.
