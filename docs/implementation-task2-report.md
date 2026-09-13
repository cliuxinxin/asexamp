# Task 2 — shared project facts and clarification adoption

Implemented within the existing source/chunk repository and clarification draft operations. No new service, database, authorization system, model gateway, or dependency was added.

## Changes

- Existing clarification sources retain their IDs and exact chunk references. Additive public metadata records `status` (`confirmed`, `provisional`, `superseded`), `scope` (`module`, `version`), question-level claims, provenance, and replacement relationships. Historical source evidence remains readable.
- `shared_sources(store, project_id, scope=None)` returns active confirmed project facts, with optional structured scope filtering. Legacy records remain readable. `shared_context` keeps its existing keys and adds `fact_history` plus fact metadata.
- `share_clarification` accepts optional scope, rule topic, explicit predecessor IDs and provenance. Same-topic contradictions in overlapping scopes raise a narrow `FactConflict`; explicit replacement atomically retires the prior shared source. Other projects and unrelated modules cannot be substituted.
- `save_draft` immediately adds submitted answers to the current run and shares confirmed conclusions by default. Explicit `status='provisional'` / `provisional=True` records a task assumption that cannot be shared or retire a confirmed rule. Explicit `share=False` / `save_to_project=False` remains available. Saving does not approve a workflow gate.
- Draft responses include pending and resolved question rows. A newly reduced/changed question set starts a new submission while retaining previous submission source IDs; answering remaining questions does not remove prior conclusions. Edit leases accept `_edit_token` or `edit_token`.
- `project_facts.CAPABILITIES` and `async execute(..., turn_id=None)` provide `project.confirm_fact` and `project.list_facts`, with command receipts, actual message provenance, deterministic retry identity, project validation and rollback of conflicting writes. Conflict receipts include a concrete `supersedes` continuation.
- `ProjectSamples.tsx` exports `ProjectFacts({projectId})`. The existing project UI shows confirmed rules, module/version, confirmer/time, source conversation/message/task, and replacement history. Removing sharing keeps evidence intact. Existing sample pinning, protected manual fields, template family boundaries and snapshot stripping are retained.

## Integration contracts

- Controller registers the new module's capabilities and adapter. The adapter does not call a model.
- Clarification workflow owns refreshing the related understanding artifact and moving answered clarification to the understanding confirmation gate. The fact/draft domain operation itself preserves the run's waiting gate.
- Clarification capability schemas accept `status`, `provisional`, `scope`, and `supersedes`. `FactConflict.conflicts` contains the exact source ID, key, scope, current value and proposed value.
- New-run source collection excludes provisional sources, including later tasks in the same conversation. Their existing run explicitly retains the evidence.
- Existing `Store.put` source version updates and immutable evidence snapshots are retained. Confirmation metadata changes can advance the source version; original IDs/chunk refs and prior evidence snapshots remain readable. No source-version guard is weakened.
- `shared_sources` does not infer module/version from free text. Sources created by the new chat and draft operations explicitly include the applicable scope in their evidence text; callers with known structured scope can pass it directly.

## Verification

Used the test-driven-development and verification-before-completion workflows. Initial new regressions reproduced missing scope arguments, missing provisional metadata, and the previous unshared submission default. The focused suite now has eight real SQLite tests covering cross-chat reuse, project isolation, exact refs, scope selection, suggestions and assumptions, same-chat future task exclusion, explicit replacement/history, narrow conflict rollback, command retry provenance, partial clarification rounds, and provisional edits preserving confirmed facts.

Commands run:

```bash
PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-test-env/bin/python -m pytest -q tests/test_project_facts_v290.py tests/test_conversation_project_v260.py tests/test_project_context_v2512.py tests/test_conversation_clarification_v260.py -k 'project_fact or pin or template or shared_clarification_reused or project_isolation or edit_confirmed_fact or full_textarea or stale_revision_question'
# 19 passed, 11 deselected

cd frontend
node node_modules/typescript/bin/tsc --noEmit
# exit 0
```

Running the two complete older unittest modules also exposed three outside-scope assertions: an AST-only workflow fixture lacks the current `validate_confirmation` global; a control fixture expects a now-absent `input_version`; and an old draft assertion expects `saved.shared == False`, contrary to the approved v2.9 default. These historical tests were not edited or represented as passing. Current clarification flow integration is covered separately by Task 5.

Final integration rerun added `tests/test_clarification_flow_v290.py` and `clarification_flow` to the above selection: **23 passed, 11 deselected**. One third-party Starlette/AnyIO deprecation warning was emitted. `git diff --check` passed for the owned files.

## Controller cross-review

A bounded review used the production ConversationController, WorkflowEngine, LangGraph and SQLite with controlled external model decisions/responses. The scratch experiment also checked fact conflict → `conversation.resolve` → idempotent retry, provisional facts followed by `workflow.continue` in the same turn, another chat's unrelated fact during Auto generation, and cancellation of a deferred fact write.

Two actual blockers were reproduced and fixed by Task 5:

1. Replacing a consumed project fact in another chat changed only the old source's recorded supersession lifecycle but caused an already-running scenario model call to fail with `DependencyConflict` / “依赖已改变，请重新生成”. The fix verifies the exact old immutable evidence and allows only the recorded retirement transition; it retains old references and leaves the ongoing task's sources unchanged.
2. A confirmed project fact conflict unrelated to the current Auto task was treated as a task edit, so its unresolved question paused Auto at `workflow_paused(reason=write)`. The adapter now owns whether an operation requires a task boundary. Confirmed project facts remain writes with cancellation and receipt protection but do not pause the current task; provisional assumptions still require a boundary.

Both failures were captured RED in `tests/test_project_fact_isolation_v290.py` before their production fixes. Their GREEN rerun, four negative source bridge checks (text, chunks, scope, role), the existing ordinary deactivation guard, and the fact/input suites produced **19 passed**. The provisional + continue experiment reached the next scenario gate without stale confirmation errors: this source-only operation changes `input_version`, not the pending artifact revision or control binding.

A further prompt/API mismatch was reproduced: the turn prompt's `provisional:true` spelling was ignored by `project.confirm_fact`, which previously read only `status`. A controlled routing response using that spelling incorrectly saved a shared confirmed fact. `test_prompt_provisional_alias_never_escalates_an_assumption_to_shared_fact` captured that failure RED. Task 5 added common normalization for execution and boundary policy; `provisional:true` now preserves the task-assumption restriction. Its GREEN rerun and all relevant fact/isolation/input checks produced **20 passed**, with the same third-party deprecation warning:

```bash
PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-test-env/bin/python -m pytest -q tests/test_project_fact_isolation_v290.py tests/test_conversation_inputs_v290.py tests/test_project_facts_v290.py --tb=short
```

The review changed only this report and the new isolation tests. Production fixes and their commits belong to Task 5; no failing guard was bypassed. The routing model was controlled, so these checks establish capability execution and prompt/API consistency, not the accuracy of an external model's interpretation of arbitrary language.
