# Incremental Agent Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for isolated module tasks and inline execution for the integrated runtime. Track concrete validation below.

**Goal:** Run useful test-design work in small durable units with on-demand evidence, automatic continuation, exactly one review stage and observable recoverable progress.

**Architecture:** Keep the local FastAPI/SQLite/model adapter. Introduce a workspace for durable work units and bounded contexts, and an incremental LangGraph executor that checkpoints each unit. Reuse the existing artifact, evidence, schema and field-repair services; emit accepted previews through the existing SSE feed.

**Tech Stack:** Python, FastAPI, LangGraph, LangChain, SQLite, React, TypeScript.

**Spec:** `docs/superpowers/specs/2026-09-08-incremental-agent-design.md`.

## Global Constraints

- User clarification: old tasks/checkpoints need no migration; retain model configuration, source documents and saved artifacts.
- Keep `.env`, custom Headers, minimal Chat Completions and 3600-second model timeout.
- No external embedding/vector service, deployment or private model credentials.
- Every accepted work result is durable and idempotent; a failed unit must not rerun earlier successful model calls.
- No source truncation may be disguised as complete coverage; show partial results as work previews.
- Auto must not pause on nonfatal business questions; explicit continue must be honored.
- Generate Case includes exactly one AI review stage, possibly batched; no score threshold or whole-set regeneration.
- Never put test-only switches in production code. Use controlled model adapters in tests.
- English code comments; Chinese product explanations. Do not expose raw chain of thought.

### Task 1: Durable workspace and bounded context primitives

**Files:** create `backend/tcg/incremental_workspace.py`, `tests/test_incremental_workspace.py`.

**Interfaces:**

```python
def fingerprint(value) -> str: ...
def split_evidence(evidence: list[dict], budget: int = 6000) -> list[list[dict]]: ...

class Workspace:
    def __init__(self, store): ...
    def units(self, run: dict, budget: int = 6000) -> list[dict]: ...
    def begin(self, run_id: str, key: str, kind: str, title: str, refs: list[str] = (), dependencies: list[str] = ()) -> dict: ...
    def get(self, run_id: str, key: str) -> dict | None: ...
    def accept(self, run_id: str, key: str, result: dict, artifact_id: str | None = None) -> dict: ...
    def fail(self, run_id: str, key: str, error: str) -> dict: ...
    def list(self, run_id: str) -> list[dict]: ...
    def view(self, run_id: str) -> dict: ...
    def model_context(self, run: dict, task: str, data: dict, budget: int = 16000) -> dict: ...
```

`units` returns `id`, `title`, `evidence`, `refs`, `source_ids` for every non-example paragraph in current authorized sources. IDs are stable content hashes. Oversized paragraphs are split into complete offset-annotated parts retaining original reference IDs, never rejected or silently truncated. Location/source role are retained. Group by source and paragraph boundaries with a serialized budget. Only the specific source is read by work on that source.

Persist `agent_work` records using existing Store object storage. Keys are scoped to run ID; completed records have private `_result` and optional artifact_id. Idempotent accept cannot overwrite a completed result. `view` returns `{completed,total,current,items}` with no raw result/body; items contain id/key/kind/title/status/refs/artifact_id/attempt/error. begin retries failed/running items but does not change completed ones. All mutations check running status and share transactions with events.

Context contains only supplied data plus a small task-specific Profile subset, goal and depth. Profile mapping: analyze scope/language/additional_rules; scenarios scenario schema/level and additional_rules; cases case schema/level/types/template and additional_rules; review review_rules/dimensions/schema; query no unrelated config. Preserve supplied required business values, reject oversize with actionable DomainError. Do not silently truncate rules. Exclude run history, full artifacts, unrelated profile fields by default. Do not call the model or implement runtime routing in this module.

- [x] Write failing tests using Store and temporary directories:

```python
def test_large_paragraph_parts_reconstruct_original(store_run):
    groups = split_evidence([store_run.evidence], budget=1200)
    assert ''.join(e['text'] for group in groups for e in group) == store_run.evidence['text']

def test_accept_is_durable_and_idempotent(workspace_run):
    ws, run = workspace_run
    ws.begin(run['id'], 'analysis:one', 'analysis', 'Login')
    ws.accept(run['id'], 'analysis:one', {'items': ['kept']})
    assert ws.begin(run['id'], 'analysis:one', 'analysis', 'Login')['_result'] == {'items': ['kept']}
```

- [x] Run `PYTHONPATH=backend ../tcg-local-venv/bin/python -m pytest tests/test_incremental_workspace.py -q`, confirm RED.
- [x] Implement the listed interfaces and tests for run isolation, restart, failure, body-free public view, source completeness and context budgets.
- [x] Run the same command, inspect GREEN and commit only owned files.

### Task 2: Incremental execution, small replies and correct business gates

**Files:** create `backend/tcg/incremental_agent.py`, `backend/tcg/incremental_tasks.py`, `backend/tcg/incremental_contracts.py`, `backend/tcg/runtime_version.py`, `tests/test_incremental_agent.py`; modify `graph.py`, `main.py`, `storage.py`, `model.py`, `schemas.py` as required.

**Interfaces:** consumes Workspace. Engine uses the new executor for new agent tasks. Each iteration performs one persisted work unit; state holds IDs/cursors. Existing artifact APIs remain the boundary for accepted previews and final output. User instructions enter the same executor and invalidate dependency-linked units, not all state.

- [x] Reproduce failures before implementation with real API/graph tests:

```python
def test_auto_questions_continue_and_exactly_one_review(client_with_model):
    client, model = client_with_model
    run = run_case_task(client, mode='auto')
    assert run['status'] == 'completed'
    assert [task for task, _ in model.calls].count('review_cases') == 1
    assert not any(task in ('agent_plan', 'agent_summary') for task, _ in model.calls)

def test_failed_later_unit_reuses_accepted_units(client_with_model):
    client, model = client_with_model
    run = run_large_task_with_second_unit_failure(client)
    before = model.accepted_input_call_counts.copy()
    retry_current_unit(client, run)
    assert model.accepted_input_call_counts == before
```

- [x] Implement select/execute/gate/finish checkpoints; local known successors, combined intent/depth choice only when needed, small analysis units and immediate preview artifacts.
- [x] Implement current-unit `need_context` search/read/section navigation with bounded results and explicit continuations. Persist observations; guard repeated no-progress tool requests. Return findings and patches without global planning.
- [x] Generate from accepted local rules/scenarios, checkpoint each page, validate only the relevant scope then aggregate coverage. Split oversized evidence automatically; retain full ledger through the final source.
- [x] Add targeted review of generated/imported cases using one logical review stage. Apply operations with stable IDs and scope guards. Preserve review results over retry, validate final links/coverage.
- [x] Implement Auto/confirmation/continue, pause/resume, instruction impact with stable source units and targeted dependencies. Reuse authorized accepted facts across runs using source/model/config fingerprints; explicit artifact revisions constrain edits.
- [x] Build final summary from accepted business summaries and local counts; do not make a final LLM call. Do not silently publish incomplete results.
- [x] Add startup build fingerprint/version to health and diagnostics, and work list/progress to run responses; preserve secrets and existing transport configuration.
- [x] Run new integration tests and relevant existing schema, transport, lifecycle and API suites; revise obsolete behavior expectations using the approved spec rather than weakening assertions.

### Task 3: Live work and usable controls

**Files:** `frontend/src/AgentPanel.tsx`, `RunCard.tsx`, `RunTimeline.tsx`, `types.ts`, `App.tsx`, `styles.css`, frontend tests.

**Interfaces:** run.agent.work contains Workspace.view; work items have id/key/kind/title/status/refs/artifact_id/attempt/error. Every work update is in the existing `agent` SSE event. GET `/api/runs/{id}/work` returns the complete paginated work inventory with cursor. POST `/api/runs/{id}/pause` pauses and POST `/resume` continues a work pause. Artifact IDs open existing artifact viewers. Backend emits `agent.preview_ids` for the latest accepted partial analysis/scenario/case artifacts.

- [x] Write failing interaction tests: Auto selected by default sends `mode:auto,confirm_strategy:false`; user enables confirmation sends `mode:hitp,confirm_strategy:true`; partial results are labeled working drafts and openable; work errors/retry do not appear as endless waiting.
- [x] Render current work, counts, source references and expandable completed work. Retain request inspection, responsive chat and depth Auto option. Show pause/resume and distinguish upstream nonstreaming wait from real model deltas.
- [x] Run frontend tests and build, then inspect the live UI and console. Commit source/tests and rebuilt assets.

### Task 4: Integrated verification and GitHub delivery

- [x] Run complete backend/frontend tests, build distributable assets, perform a controlled multi-module end-to-end run and inspect request sizes/call counts and preview events.
- [x] Review the entire diff for evidence scope, partial publishing, retry/idempotency, instruction invalidation, mode semantics and model-gateway compatibility. Address concrete findings and rerun covering tests.
- [x] Document measured controlled results and remaining real-provider limits. Update the GitHub implementation branch/PR with the exact tested tree. No deployment; user will test locally.

## Progress

- Initial controlled baseline from diagnosis: simple generation 8 model calls, 4 planner calls, no mandatory review; Auto questions wait.
- User explicitly waived old task migration during implementation; no migration/replay subsystem will be built.

## Verification record

- Backend: `PYTHONPATH=backend python -m pytest tests -q` — 319 passed; one existing Starlette/AnyIO deprecation warning.
- Frontend: `npm test` — 45 passed; `npm run build` completed, existing Mermaid chunk-size warning.
- New runtime tests use the real default app and controlled models. Four historical v2 test modules explicitly select their retained interpreter in a tests-only harness; their results are not v3 product acceptance.
- Independent review regressions cover dependency excerpt scope, rechecking invalidated graphs, cumulative type coverage, and preserving accepted changes/adds/deletes across instructions.
- Live UI and GitHub delivery are verified separately before final delivery. No real internal-provider latency/quality benchmark or old-task migration was performed.
