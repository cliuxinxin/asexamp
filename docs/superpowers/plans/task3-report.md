# Task 3 — durable conversation Turn and capability contracts

Implemented a real persistent LangGraph Turn using a dedicated lazily opened AsyncSqliteSaver. Each Turn uses its deterministic message-derived ID as thread_id and records graph version turn-v270. Graph nodes own plan, one-command execution, result projection, bounded followup and durable waiting; the old Controller execution loop and same-Turn recursion are removed. Controller retains request deduplication, capability adapters, target/run binding, cancellation, result projections and safe-boundary scheduling.

Checkpoint state contains only turn_id, command_id and route. Business command receipts remain the effect authority. Successful effects and prepared previews replay from persisted receipts across the gap between transaction commit and graph checkpoint. Waiting input, deferred edits and preview confirmation use real LangGraph interrupts and resume commands. Reopening the Store and Controller restores the same checkpoint. Node cancellation wrapped by LangGraph is unwrapped to preserve cancellation behavior. Saver shutdown is included in Controller.close.

Capability definitions now own target_types, target_required, context_policy and optional preparation. Review optimization's dynamic write effect belongs to the review capability, including at initial command registration so cancellation sees it correctly. The target resolver consumes metadata, including for third-party capability names. Legacy custom registries inherit builtin metadata without changing execute signatures. The central prompt expresses generic contracts; business behavior stays in capability descriptions. conversation.catalog expands artifact, item, source, pending, active-command/Turn and clarification-question metadata in pages of at most 50. Routing catalogs disclose total counts and partial flags; catalog results retain structured metadata across followup planning.

Workflow continue binds the actual interrupt, artifact revision and control version. It validates the supplied binding before any explicit scope change and forwards the resulting current control token, so a combined scope+continue command does not invalidate itself. Short acknowledgments following read results cannot approve old pending proposals. Existing adoption/submission/sharing and safe-boundary behavior are preserved. Readonly case review and comparison now call context_service.artifact_context; ordinary analysis/estimate uses the artifact_actions hook owned by root.

## Verification

Runtime: real restored dependencies at /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python with PYTHONPATH=backend.

Final focused run:

```
python -m pytest -q tests/test_framework_turn_v270.py tests/test_conversation_v260.py tests/test_conversation_review_v260.py tests/test_conversation_project_v260.py tests/test_conversation_clarification_v260.py --show-capture=no
```

Result: **44 passed in 1.97s**. git diff --check passed for all owned files.

New integration tests exercise real checkpoint recovery after a committed write and process reopening, ordered compatible commands, actual durable waiting/selection resume, prepared-preview receipt replay, snapshot reads leaving Run state unchanged, unrelated affirmative nonapproval, capability-owned targeting/effect preparation and bounded catalog expansion. Existing controller review tests cover deferred writes, cancellation, preview compound tails, scoped followups and cancellation after a committed effect.

An earlier broader run (while root integration was in progress) had 44 passes and three real workflow gate failures before the expected waits; the prior underlying trace was source-role validation in operations.revise_artifact called by root's new annotate_artifact path. Reported to root for integrated verification. Legacy test_conversation_artifacts_v260.py also contains top-level async helper functions that pytest collects without their data fixture; its unittest class must be selected explicitly. A legacy test mutating an immutable artifact profile in place needs fixture migration. These are separate from the passing Task3 suite and are not claimed resolved here.

No intranet model was contacted. Controlled responses validate program behavior, not live model quality.
