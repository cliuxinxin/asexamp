# Native Agent + Pipeline Implementation Plan

> Execution: parallel bounded implementation in the current session; user supplied and approved the architecture. No additional approval gate.

**Goal:** Replace custom conversation planning and pause flags with native LangChain tool calling and a pure checkpointed LangGraph pipeline, preserving the React API and domain artifacts.

**Architecture:** NativeChatAgent receives each turn and runs a standard model/tool loop through LangChain create_agent. Typed tools call NativeBusiness and PipelineRuntime. The pipeline has separate work and interrupt nodes. Store remains the authoritative versioned artifact/evidence database; native checkpoint state contains references only.

**Tech stack:** Existing pinned LangChain 1.4.0 / LangGraph 1.2.11 / SQLite / FastAPI / React. Add standard jsonschema for function argument validation.

**Spec:** User-provided TCG Case Agent 核心架构重构方案, this conversation 2026-09-12.

## Constraints

- Keep POST /api/chats/{id}/turns and TurnResponse status/message/parts/actions/pending.
- Keep frontend layout and code behavior. Native pending prompts must retain the existing projection shape.
- Do not execute the deleted ConversationController or TurnGraph. No workflow boundary nodes, synthetic detours, or persisted edit/control locks on new runs.
- LangGraph resumes a node from its beginning: work/save and interrupt are separate nodes.
- Use CAS revisions and SQLite transactions for data concurrency; a short-lived per-chat execution mutex prevents overlapping local writers. This mutex never represents workflow waiting and is not persisted.
- Function calls carry typed arguments. Do not parse assistant prose as an actions/operations JSON envelope.
- Requirements, scenarios and cases keep stable IDs, exact evidence and lineage. Manual execution fields are protected.
- Capacity is decided by the server; safe batches split only after actual context-capacity errors.
- Existing artifacts and project facts remain readable. Old checkpoints must be explicitly migrated or retained as historical; no guessed auto-approval.

## Deliverables

1. Native model adapter: chat_model() exposes bind_tools; generate_native(task, context, schema, instruction) accepts exactly a declared result tool. Preserve provider auth and output settings. Test wire-level tool_calls and no JSON prose fallback.
2. NativeBusiness: understand/scenarios/cases/review plus clarify/revise/estimate/analyze. Typed submitted rows, server-computed diffs, protected data, saved lineage. Test evidence and affected-row preservation.
3. PipelineRuntime: separate generation/gates, real interrupt and Command(resume), checkpoint-derived pending identity, Auto/Human, restart and latest-data reads. Test no generation replay and one-stage confirmation.
4. Tool Registry: typed @tool functions, chat/project scope, declared write sessions, frozen prompt binding, templates, exports, project clarification. Test current-target guards and required business capabilities.
5. NativeChatAgent/API: native create_agent, contextual artifact directory and current checkpoint prompt, API response assembly and idempotent client request handling. Delete obsolete custom controllers; adapt read-only views and migration.
6. Continuous HTTP verification: actual BaseChatModel tool calls over real SQLite/LangGraph, generate/pause/read/modify/resume; package startup, unchanged frontend build, code cleanup and release ZIP.

## Verification

Run the focused native test files, a continuous Human and Auto journey, and frontend production build. Verify the release contains no active import of the old controller, turn graph, workflow boundary, or JSON action-plan parser. No claims about real internal model behavior without connecting to it.

## Continuation audit — 2026-09-12

Deliverables 1–6 were integrated in commit `4c39da9`; the continuation started from that completed implementation, not from another design round. A fresh backend run passed 69 tests, and a read-only approval audit found no defects in the four Human gates, revision rebinding or one-prompt-per-assent guard.

The remaining integration gap was the diagnostic producer contract: native model/pipeline event names did not match the unchanged React progress reducer. The follow-up repair emits the existing model and node lifecycle events and archived request/output metadata, with no new workflow state or UI layout. Final results are recorded in `docs/VALIDATION-v3.0.0.md`.
