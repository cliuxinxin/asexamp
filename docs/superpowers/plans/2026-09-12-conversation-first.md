# TCG v2.9.0 Conversation-first Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development with bounded ownership and task review. This plan is already authorized; continue through implementation and delivery.

**Goal:** Implement the approved conversation-first design while preserving reliable Auto/HITP generation and existing data.

**Architecture:** Reuse the existing conversation controller, capability registry, LangGraph workflow, artifact revisions and context builder. Targeted source adoption and project facts share these domain operations. The conversation becomes the primary surface, with one active editable artifact and inline lineage.

**Tech Stack:** Existing FastAPI/Python, LangGraph, SQLite, React/TypeScript/Vite. Preserve pinned dependencies.

**Spec:** `docs/TCG-CONVERSATION-FIRST-DESIGN-2026-09-12.md`

## Global Constraints

- Preserve stable IDs, exact evidence refs, immutable revision history and string `scenario_id`.
- Human gates: understanding, scenarios, case draft, reviewed result. Editing saves without advancing; explicit compound continuation advances only its authorized gate.
- Auto retains its original goal after a safe-boundary edit unless the user requests a hold.
- Source uploads alone do not adopt documents or block unrelated work. Adopt only specified sources into specified artifacts/rows.
- Confirmed project facts are shared by project and scope; suggestions and provisional assumptions remain separate.
- Read-only estimates/explanations/summaries never generate cases or approve gates.
- All model calls use the existing bounded context/receipt pathway.
- Reuse the original execution-agent-UI visual reference; white sidebar, gray workspace, red emphasis, compact composer. No new visual concept.
- Validate relevant contracts and continuous journeys, not the entire historical suite.

## Task 1: Targeted source adoption and lineage

**Files:** `backend/tcg/workspace_changes.py`, `conversation_workspace.py`, `conversation_project.py`, `artifact_actions.py`, `workspace_coverage.py`, `dependencies.py`; focused new tests `tests/test_targeted_sources_v290.py`.

**Interface:** Preserve `execute`, `prepare_reconciliation`, `assert_current_inputs`, and current HTTP routes. Extend existing result dictionaries additively. Use `artifact_id`, `expected_revision`, `selected_ids`, `source_ids`, `sync_targets`; do not retarget downstream requests to analysis. Persist actual source adoption and upstream discrepancies with revisions. Expose discrepancy detail in the artifact workspace response for row presentation.

- [x] Read existing source-impact, local revise, atomic apply and source guard tests.
- [x] Add regression fixtures where a waiting case gate receives a new unadopted source and still continues; where direct case source update preserves unrelated rows and records upstream discrepancy.

```python
assert_current_inputs(store, waiting_run)  # unrelated upload must not raise
assert saved_case['items'][1] == before_case['items'][1]
assert saved_case['revision'] == before_case['revision'] + 1
assert analysis_after['revision'] == analysis_before['revision']
```

- [x] Run the focused regressions to capture the missing behavior.
- [x] Route explicit downstream source requests through existing scoped edit/sync operations; record adopted source IDs only on actual commit, preserve conflict/late-result checks, and narrow current-input checks to actual consumption.
- [x] Add visible structured dependency conflict details and inline upstream discrepancy state without inventing parent links.
- [x] Re-run focused source/lineage tests and record results.

## Task 2: Shared project facts and current clarification adoption

**Files:** `backend/tcg/project_context.py`, `clarification.py`, new `project_facts.py` if useful; `frontend/src/ProjectSamples.tsx` or a focused project fact view; focused tests `tests/test_project_facts_v290.py`.

**Interface:** Preserve `shared_sources`, `share_clarification`, `shared_context`, `save_draft` callers. Extend records with status, scope, provenance and supersession, with backwards-compatible reads. Provide a bounded project fact capability adapter for controller registration by Task 5; no edits to Task 1-owned `conversation_project.py`.

- [x] Add behavior checks for cross-chat confirmed fact reuse, provisional assumptions staying unshared, scoped replacement and conflict preservation.

```python
assert rule['project_id'] == project['id']
assert rule['status'] == 'confirmed'
assert suggested_id not in {s['id'] for s in shared_sources(store, project['id'])}
assert old_rule['id'] not in {s['id'] for s in shared_sources(store, project['id'])}
```

- [x] Run these tests before implementation.
- [x] Use the existing source IDs/chunks as business evidence; store structured fact metadata and keep superseded snapshots readable by old artifacts.
- [x] Make answering the current clarification immediately usable by that task while preserving stage confirmation. Explicit future replacement is accepted; unresolved conflicts produce a narrow question.
- [x] Preserve Profile sample stripping/snapshot rules and expose confirmed fact provenance in the existing project UI.
- [x] Re-run relevant project/clarification tests.

## Task 3: Conversation-first UI and compact input

**Files:** `frontend/src/App.tsx`, `ArtifactWorkspace.tsx`, `styles.css`, optional `conversation-layout.css`, `taskPrompts.ts`; focused `frontend/tests/conversation-layout-v290.test.tsx`.

**Interface:** Preserve `ArtifactCard` props and conversation command contracts. Task 4 owns card contents. Only one live editable artifact surface; historical cards open explicit snapshots. Confirmation/selection stays bound to actual run/revision.

- [x] Read existing App/run/phase-selection tests and original UI reference tokens.
- [x] Assert compact composer and primary conversation structure; assert selected row ID/revision reaches a turn; assert stage confirmation is unavailable during editing.

```typescript
assert.equal(screen.getByLabelText('聊天输入').getAttribute('rows'), '2');
assert.equal(screen.getAllByRole('checkbox', {name: '选择 S-2', exact: true}).length, 1);
assert.deepEqual(sent.selected_ids, ['S-2']);
assert.equal(sent.artifact_revision, current.revision);
```

- [x] Implement wide conversation, embedded expandable artifacts, reversible focus view, concise stage actions and compact selection/source chips.
- [x] Keep source upload messaging neutral about target; task shortcuts insert editable prompts without replacing a typed draft.
- [x] Apply the source visual styles without moving all content to a red background; responsive sidebar, table local scroll and no empty stretched composer.
- [x] Run focused interaction tests and production build when runtime is ready.

## Task 4: Inline lineage and case review table

**Files:** `frontend/src/ArtifactCard.tsx`, `WorkspaceCoverage.tsx` (only if needed for reuse), new `ArtifactLineage.tsx`, new scoped CSS; focused `frontend/tests/artifact-lineage-v290.test.tsx`.

**Interface:** Consume existing `/artifacts/{id}/workspace` lineage; tolerate additive discrepancy metadata from Task 1. Preserve current parent relation shape and fetch once per artifact/revision. Do not edit App or shared styles owned by Task 3.

- [x] Add tests that scenario rows show requirement ID/title, case rows show scenario and requirement, and step/expected stay paired in review.

```typescript
assert.ok(within(row).getByText('R-1'));
assert.ok(within(row).getByText('S-1'));
assert.ok(screen.getByRole('table', {name: 'C-1 步骤与预期结果'}));
```

- [x] Implement inline lineage, expandable exact parent content/evidence, pending synchronization labels and missing-parent states.
- [x] Make the review table show preconditions, first step/expected and expandable full steps plus actual review findings; keep custom fields accessible.
- [x] Replace the separate coverage block with inline row state and concise current-scope counts/filtering. Do not label linked rows as semantically verified.
- [x] Retain selection, direct edit, diff/undo, sample pinning, scenario/case exports and protected fields.
- [x] Run relevant card/review interaction checks.

## Task 5: Control semantics, integration and delivery

**Files:** `backend/tcg/conversation.py`, `conversation_context.py`, `conversation_workflow.py`, `workflow.py`, `prompts.py`, `graph.py` only where required; new `tests/test_conversation_journey_v290.py`; version files and demo docs.

**Interface:** Register Task 2 project facts capability and refresh bindings only from actual committed results. Retain `TurnRequest`, explicit source IDs and pending IDs. Coordinate Task 1/3/4 additive types without changing core schemas.

- [x] Write a continuous HTTP journey using existing `Journey` helpers and a controlled external model, exercising all four gates, estimate-only, edit-hold, selected-source update, linked sync, review, facts and export.

```python
assert run['status'] == 'waiting'
assert run['interrupt']['type'] == expected_gate
assert cases_after_estimate == cases_before_estimate
assert generated_context['scenarios'] == confirmed_scenarios
```

- [x] Ensure free text takes the same path as buttons; active task questions do not resume interrupts; explicit compound continuation binds its resulting revision; safe-boundary edits preserve Auto goals and user holds.
- [x] Reuse actual context budgets, source manifests and receipt idempotence; improve conflict copy where existing errors leak a generic failure.
- [x] Perform task-scoped review and fix concrete integration findings, then run focused backend/DOM checks and build.
- [x] Validate actual rendered UI through supported Browser if available. If navigation is blocked, report the limitation and continue all other authorized validation without bypassing browser policy.
- [x] Update version to 2.9.0; include demo input documents and a concrete sequential demo guide; package source plus production build, exclude environments/caches/local data; save final ZIP and demo guide.

## Progress

- Baseline: isolated copy from v2.8.0; original package and approved design retained.
- Ownership: Tasks 1–4 use disjoint production files; Task 5 owns controller integration. Shared-interface changes are communicated before edits. Runtime setup is independent.
- Preflight: all tasks retain the same artifact IDs/revisions and parent field contract. Task 1 emits additive workspace metadata consumed by Task 4; Task 2 emits a capability registered by Task 5; Task 3 retains Task 4 component props. No new backend service or dependency is required.

## Integration verification (2026-09-12)

- Backend final targeted suite: 103 passed; DOM suite: 48 passed; TypeScript and production build passed.
- Real continuous HTTP Human/Auto journeys use only controlled external model replies; no injected artifact/gate records.
- Cross-review fixed project-rule replacement isolation, unrelated fact writes pausing Auto, and provisional spelling compatibility; strict evidence negative checks retained.
- Historical message revisions and focused full-step expansion are connected.
- Browser localhost navigation returned ERR_BLOCKED_BY_CLIENT; visual validation remains unavailable and is explicitly documented.
- Demo: docs/DEMO-v2.9.0.md and examples/conversation-v290; original templates retained.
- Final packaging checks and durable save are recorded in the release manifest and delivery receipt.
