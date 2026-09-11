# Final integration review — v2.7.0

Scope: approved `TCG-WORKFLOW-FRAMEWORK-ARCHITECTURE.md` and implementation plan; changes from `49c24b2` through the current integration working tree. The review covers fixed Run integration, the persistent Turn graph, generation/context commit boundaries, linked artifact actions and confirmation versions. The concurrent source-impact renderer is excluded. This is a targeted review, not a historical full-suite run or actual model endpoint validation.

## Final verdict

**No remaining Important or Critical finding in the reviewed scope.** All six confirmed Important findings below have been addressed in the integration working tree. The reviewer reread the fixes and independently ran 25 focused checks covering candidate grouping, generation freshness, waiting-gate versions, persistent Turn replay, own-write continuation and context receipts. The broader release gate remains the root agent's responsibility.

## Closed findings

1. **Context admission prevented capacity grouping — resolved.** `context_service.artifact_context` raised when the whole candidate did not fit, before `artifact_actions.bounded_groups` and `FlowEngine.capacity_groups` could split it. A real-Store probe with two scenario rows established that each row fit individually for explanation and case generation while both grouping entry points failed. Candidate mode now returns oversized mandatory context for grouping; final/single-item admission still rejects missing capacity without dropping necessary dependencies. The reviewer ran the integrated candidate regression successfully and checked that the grouped read-only review callsite also selects candidate mode.

2. **A committed proposal application could strand its original Turn on replay — resolved.** `ConversationController._execute` returned an already succeeded `artifact.apply` receipt before reconciling the original pending Turn. A durable continuation record now retains the parent, pending and result offset; succeeded receipt replay reconciles it. Real Store/LangGraph reopen tests pass for crashes after application, during the parent tail, and after the tail finishes before acknowledgment. Each mutation executes once.

3. **A compound write then continue rejected its own new revision — resolved.** Artifact commits advanced the waiting Run's confirmation revision/control version, but remaining actions retained the original bindings. `command_owner` now associates atomic before/after gate transitions with the owning command; exact matching transitions advance its Turn and an applied proposal's parent. Tests pass for direct write and preview/apply continuation, including rejection of external pauses and external control-version changes without a conversation epoch change.

4. **Legacy waiting edits overwrote refreshed confirmation tokens — resolved.** `Engine._edit_waiting` loaded a Run before the revision commit, then saved that stale value after `refresh_confirmation` advanced its tokens. An actual `Engine.edit_waiting` probe produced artifact revision 2 with gate revision 1/control version 0. The path now reloads the Run after the artifact transaction's refresh before clearing its edit token. The reviewer ran the corresponding regression successfully.

5. **Input-resolution replay lost the pending it had already resolved — resolved.** `ConversationController._resolve` marked a pending resolved before awaiting the original Turn, without a durable resolver binding. It now atomically saves the resolver's pending/parent reference and part offset alongside the argument update, then persists the returned response. Reopen tests pass for crashes after the parent's command receipt and after the original Turn completes before the resolver returns; the chosen command executes once.

6. **Interleaved template completion reset generation freshness — resolved.** The generation epoch originally included the model task name; `complete_case_fields` therefore discarded the earlier `generate_cases` guard. The epoch now retains earlier guards and consumed inputs across tasks within one semantic input version/edit token. The reviewer ran the focused source-change regression successfully.

## Validation boundary

The review reproductions use temporary real Stores and the specified Python environment at `/workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python`. The Turn crash probes use actual LangGraph execution and controlled adapters. Integration test reports supplied by the root agent are context, not a substitute for rereading the fixes. No production data or application code was changed by this reviewer.

Fresh reviewer command: `PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python -m pytest -q tests/test_framework_context_v270.py::test_candidate_context_allows_artifact_and_workflow_capacity_grouping tests/test_framework_generation_v270.py` → **4 passed**.

Fresh reviewer command: `PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python -m pytest -q tests/test_framework_workflow_v270.py::test_waiting_ai_edit_preserves_new_confirmation_binding` → **1 passed**. One existing Starlette/AnyIO deprecation warning.

Fresh reviewer command: `PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python -m pytest -q tests/test_framework_turn_v270.py tests/test_framework_context_receipts_v270.py` → **20 passed**.
