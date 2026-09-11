# Task 3 follow-up: Turn context provenance receipts

Added context_receipts.start(store, task, context, *, binding, call_id, budget=None), complete(store, receipt_id, status, *, error=None, usage=None), and list_turn_contexts(store, chat_id, turn_id).

start records a safe summary before invocation: task/call/Turn/command/Run IDs; sanitized manifest references and version digests; artifact references; coverage, rule coverage and routing catalog coverage; evidence IDs/source versions; format-reference IDs; profile digest; and an explicit request-budget allowlist. Full prompts, instructions, source text, profile examples, profile snapshots, raw responses and error messages are excluded. complete saves terminal status, timestamps, exception type/status and numeric usage allowlist. Repeated starts validate context identity, terminal completion is idempotent, and listing validates chat/Turn ownership. Unbound connection-test calls create no receipt.

ConversationController now binds the actual turn_id/command_id/project_id/chat_id around planner model calls and adapter execution using Diagnostics.bind; it clears inherited run_id so safe-boundary Turn work is not accidentally attached to a parent Run. Fake engines without diagnostics use nullcontext. Nested adapters inherit the correct binding. Root owns graph.py invocation hooks and main.py endpoint integration.

Root integration:

- Inside invoke_model diagnostics binding, call start with binding=self.diagnostics.context.get(), call_id=fields['call_id'], and actual request_budget metadata.
- Call complete after success or in cancellation/failure handlers, passing the exception object as error (never serialized prompt/result data).
- Expose list_turn_contexts through the chat/Turn contexts endpoint; returned records already exclude private fingerprints.

Verification: real Store, Diagnostics and persistent TurnGraph tests:

```
PYTHONPATH=backend python -m pytest -q tests/test_framework_context_receipts_v270.py tests/test_framework_turn_v270.py tests/test_framework_impact_v270.py --show-capture=no
```

14 passed. git diff --check passed. Tests prove planner/adapter attribution, inherited context restoration, secret exclusion, safe failure recording, cross-chat rejection, deduplication and terminal-state preservation. An earlier legacy ControllerReviewTests run encountered two direct-revision fixture writes rejected by the newly strengthened Store.put contract; these were reported separately to root and are unrelated to receipt binding changes.
