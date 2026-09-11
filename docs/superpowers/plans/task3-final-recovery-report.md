# Task 3 final review fixes: continuation recovery and causal gate bindings

Artifact apply now runs receipt-safe continuation reconciliation on both normal execution and succeeded-receipt replay. A persisted per-parent outbox records pending identity, original output offset and acknowledged continuation response. Already-resolved pending records remain discoverable; retry resumes partial parent checkpoints and command receipts, while completed tails are projected without repeating their effects. A recoverable parent keeps the applying Turn recoverable. Reconciliation releases only its matching preview and does not approve a newer pending preview.

Input resolution now persists an equivalent continuation descriptor in the resolving command before changing the original pending. If a crash occurs after the selected command commits or after the original Turn completes but before the resolver returns, retry uses that saved parent/offset instead of searching only open pendings or resetting completed commands.

Controller adapter execution binds root's workflow_bindings.command_owner. Normal write projection and preview-parent continuation consume the atomic _run_binding_changes recorded by root's artifact commit path. Stored bindings advance only when run ID, interrupt ID, control version and artifact revision exactly match the transition's before state. This admits the command's own committed revision without adopting unrelated current gate/control changes. External pause epochs still cancel older queued continues.

Validation with actual LangGraph and SQLite:

- test_framework_turn_v270.py plus test_framework_context_receipts_v270.py: **20 passed in 0.86s**.
- Existing ControllerReviewTests excluding two legacy fake adapters that directly increment artifact revisions via Store.put: **11 passed, 2 deselected in 1.24s**.
- git diff --check passed.

New crash probes close and reopen Store/Controller and assert one apply mutation, exactly two immutable artifact revisions, ordered exactly-once parent tail effects and stable replayed output. They cover crash after apply receipt, after first tail receipt, after completed tail before acknowledgment, and two input-resolution crash windows. Causal tests cover direct revise/continue and preview/apply/continue, with both external pause epochs and external control-version-only changes. Root owns the atomic transition producer in workflow_bindings.py; this commit contains only the Controller consumer and owned tests/report.
