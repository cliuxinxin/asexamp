# Unified Change Workspace Implementation Plan

> **For agentic workers:** Use subagent-driven-development to implement independent boundaries; integrate and review the complete change journey before delivery.

**Goal:** One durable change state and one user-facing next action across editing, new evidence and confirmations.

**Architecture:** Reuse immutable artifact revisions, the existing preview/apply transaction and LangGraph gates. A derived workspace state owns impact and continuation eligibility; source and clarification adapters update understanding through the same drafting contracts.

**Tech Stack:** Existing FastAPI, LangGraph, SQLite, React and TypeScript; no new runtime dependencies.

**Spec:** ../specs/2026-09-11-unified-change-workspace-design.md

## Global Constraints

- Preserve project/chat boundaries, stable IDs, business evidence, untouched rows and manual execution fields.
- Editing never implies confirmation; do not reset dependency guards to force acceptance.
- Existing v2.7.1 ZIP remains unchanged; deliver v2.8.0 separately.
- Retain two-pane readability and a compact composer; render one complete editable artifact.

## Task 1: Shared change state and reconciliation

Files: workspace_changes.py, artifact_actions.py, workspace_coverage.py, tests/test_workspace_changes_v280.py.

- [ ] Reproduce scenario/analysis drift, partial sync disagreement and missing durable next action.
- [ ] Implement workspace_state(store, chat, artifact_id=None), prepare_reconciliation(store, engine, chat, args, command_id=None), assert_current_inputs(store, run).
- [ ] Extract rebind_waiting_runs(store, artifacts, source_ids=None, source_roles=None) and preserve one transaction per application.
- [ ] Verify exact rows, unchanged report-only revisions, preview freshness and no gate advancement on apply.

## Task 2: Grounded understanding updates

Files: conversation_project.py, requirement_refresh.py, tests/test_requirement_refresh_v280.py.

- [ ] Reproduce new requirements with zero existing matches and clarification saved with unchanged analysis rows.
- [ ] Implement preview_from_sources(store, engine, chat, args, command_id=None), allowing evidence-backed additions independently of existing-row scope.
- [ ] Implement refresh_clarification(engine, run_id, analysis, source_id, answer), with one focused model call and replay-safe commit.
- [ ] Verify new rules, no-impact source adoption, original row preservation and exact refs.

## Task 3: Unified workspace UI

Files: App.tsx, ArtifactWorkspace.tsx, ArtifactCard.tsx, ArtifactActions.tsx, WorkspaceCoverage.tsx, scoped styles and tests.

- [ ] Assert one editable artifact and one primary next action under stage, impact and preview states.
- [ ] Add four-stage navigation, durable impact summary, proposal application and responsive workspace/chat layout.
- [ ] Move secondary tools to menus/details and remove duplicate inline modification/confirmation surfaces.
- [ ] Validate semantic interactions and production build; record browser access limits separately.

## Task 4: Integration and delivery

Files: workspace adapter/route, conversation.py/context, workflow.py, workflow_bindings.py, operations.py, RunCard.tsx, ConversationParts.tsx and journey tests.

- [ ] Register workspace.reconcile and GET /api/chats/{chat_id}/workspace-state; include bounded impact hints in routing context.
- [ ] Wire submitted clarification updates, common manual-edit rebinding and continuation freshness guard.
- [ ] Compact chat receipts and RunCard artifact duplication while retaining current input/confirmation controls.
- [ ] Run scenario-edit and requirement-supplement journeys in Human and Auto; review code, package and write one concise demonstration guide.
