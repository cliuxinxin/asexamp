# Agent document workspace implementation plan

> **For agentic workers:** Execute inline using superpowers:executing-plans, task by task; retain review gates.

**Goal:** Fix scope-changing schema repairs and make document access bounded, reusable and visible.

**Architecture:** Local document workspace feeds task-specific contexts and exposes bounded search/read actions. Analysis batches and targeted repair results persist through the existing SQLite cache; LangGraph retains IDs/epochs.

**Tech Stack:** Python, FastAPI, LangGraph, LangChain message types, SQLite, existing React SSE frontend.

**Spec:** `docs/superpowers/specs/2026-09-08-agent-document-workspace.md`

## Global constraints

- No external vector database or embedding service.
- Preserve 60-minute deadlines, gateway modes, evidence scope and existing checkpoints.
- No private endpoint, real document text or secrets committed.
- Full design cannot silently replace exhaustive analysis with top-k search.

## Task 1: Targeted repair

Files: `backend/tcg/agent_repair.py`, `agent.py`, `agent_contracts.py`, `model.py`, `tests/test_agent_document_workspace.py`.

Interface: `repair_fragment(result, issue) -> dict` and `apply_repair(result, fragment, response) -> dict`.

- [x] Write and run a real graph regression whose original analysis has invalid description and whose old repair changes the item count.
- [x] Implement a fragment request `{path, value, validation_error}` and response `{path, value}`; replace only the selected path in a deep copy. Preserve unrelated items and graph IDs.
- [x] Record `agent.validation_failed` before repair; include safe `validation_error` in recovery. Revalidate and retain bounded attempts.
- [x] Verify rejection of wrong-path, dropped-item and changed-ID repair; verify the original field is present in diagnostics.

## Task 2: Document workspace and agent context

Files: `backend/tcg/document_workspace.py`, `storage.py`, `agent.py`, `model.py`, `tests/test_agent_document_workspace.py`.

Interface: `DocumentWorkspace(store)` with `evidence(source_ids)`, `catalog(source_ids)`, `search(source_ids, query)`, `read(source_ids, refs, budget)`.

- [x] Write failing integration assertions that planner and summary never receive full document text, and a query retrieves a tail paragraph.
- [x] Implement scoped immutable evidence caching; search tokenization includes Chinese bigrams; reads retain IDs and explicit truncation. Catalog is bounded metadata.
- [x] Add planner actions `search_documents` and `read_document`, validate tool arguments, persist observations and report progress through existing insights/SSE.
- [x] Build bounded task contexts, compact accepted artifact summaries, and direct completion when accepted output/coverage permits it.
- [x] Verify scope isolation, invalid refs, no-match behavior, repeated read reuse, and unchanged UI rendering.

## Task 3: Exhaustive batches and durable reuse

Files: `backend/tcg/agent_analysis.py`, `agent_contracts.py`, `agent.py`, `model.py`, `tests/test_agent_document_workspace.py`.

Interface: `analyze_documents(agent, state, context) -> accepted analysis`.

- [x] Write failing tests with source text exceeding the per-call budget, including a unique requirement at the tail and a stopped/restarted batch.
- [x] Partition all business chunks by serialized size, persist accepted batches under context fingerprints, namespace graph/requirement IDs during merge, and retain reviewed non-requirement refs with explicit reasons.
- [x] Complete uncovered evidence separately from schema repairs. Keep cross-batch summaries and coverage visible; fail explicitly on output/context budget exhaustion.
- [x] Verify all source segments visited, successful batches not resent after restart, epoch changes do not accept stale results, and downstream generation uses accepted facts.

### Integration review regressions

- [x] Keep citations in untouched selected-edit neighbors valid without sending their raw sources to the model.
- [x] Read every input batch before completing a case import; retain stable imported IDs and tail cases.
- [x] Prioritize citations from actual tool observations and accepted output in planner/summary metadata.
- [x] Bound nested memory, strategy and routing previews, with explicit omissions and full-context limits; retain complete stored facts.
- [x] Use the complete authorized source inventory for source-existence checks; allow uncited procedural planner observations as advertised.

## Task 4: Verification and publication

- [x] Run `PYTHONPATH=backend python -m pytest -q --tb=short`, frontend `npm test`, `python start.py --check --no-install`, and `git diff --check` with the existing environment.
- [x] Record measured context reduction and test limitations in `docs/VALIDATION.md` and explain changes in user documentation.
- [x] Obtain independent read-only review, address concrete findings and verify affected regressions.
- [ ] Commit scoped files; create GitHub tree matching local tested tree, PR and merge with exact head guard.
