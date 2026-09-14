# Artifact Workspace Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development; isolated ownership allows backend grid, proposal consolidation and frontend implementation to proceed with the contract below.

**Goal:** Deliver one workspace and one commit gateway for all business artifacts.
**Architecture:** Preserve native pipeline gates and server revision guards. Generalize the existing table projection, review state and view contracts. Collapse duplicate editing/confirmation paths.
**Tech Stack:** Python/FastAPI/LangGraph/SQLite, React/TypeScript/Vite.
**Spec:** docs/superpowers/specs/2026-09-14-artifact-workspace-design.md

## Global Constraints
- Work only in this extracted project; the original upload is unchanged until the final validated replacement.
- Backend and frontend use `/workspace-grid` with `/project`, `/export`, `/save` children.
- Existing grid fields remain; add `artifact_type` and `mode` (read_only/manual/ai_proposal).
- Canonical proposal storage kind is `artifact_proposal`; discriminator `proposal_type` is `review` or `revision`. Shared fields `artifact_revision`, `items`, `report`, `changes`, `status`, `_dependencies`, `_source_ids`, `_source_roles`; revision-specific dialogue and column_change remain optional. Preserve proposal IDs during conversion.
- Tests focus on observable workflow, real persistence and validation. Do not change dependency pins merely for the execution environment.

### Task 1: Backend grid and save gateway
**Files:** backend/tcg/table_projection.py, table_review.py, manual_edits.py (delete), tests/test_workspace_grid_v314.py. Main route integration belongs to controller/integration task.
**Consumes:** Canonical artifact proposals produced by task 2; preserve old function names such as save_review for pipeline call-site stability.
**Produces:** TableDraft/TableSave optional profile, generalized review_view/project_draft/save_review and workspace-grid routes.
- [ ] Add a focused failing scenario/manual/history round-trip test against real Store and pipeline fixtures.
- [ ] Generalize projection via artifact_table_projection; add scenario_table_projection and analysis columns. Reuse case projection/export to preserve exact existing output.
- [ ] Generalize row validation and `_prepare_resolution` by artifact type; fold manual report/column/evidence semantics into save_review; delete manual_edits.py.
- [ ] Use unified proposal discriminator and fields, preserving review gate resumption and idempotent commit.
- [ ] Run focused grid/projection/manual tests and report exact commands/results.

### Task 2: Proposal and chat/tool consolidation
**Files:** backend/tcg/review_proposals.py, native_business.py, storage.py, pipeline.py, native_views.py, native_api.py, artifact_previews.py, tool_registry.py, native_chat.py, prompts/chat.yaml, main.py startup and legacy PUT route, native_migration.py (delete); associated focused tests.
**Consumes:** Task 1 retains register_table_review_routes, save_review and new unified proposal fields.
**Produces:** One artifact_proposal object and one diff generator, compact workspace guidance and removal of old apply/discard model tools.
- [ ] Inspect actual revision proposal producers/consumers and write a focused failing test for unified storage and active proposal continuation.
- [ ] Normalize old proposal kinds once at Store startup; generate item diffs through common item_changes in review_proposals.py. Update all backend readers/writers; retain semantic public functions where useful.
- [ ] Replace model-facing apply/discard tools with workspace guidance; ensure app-driven save still resumes pipeline and clears pending prompts.
- [ ] Remove native_migration.py and startup invocation; preserve explicit handling for unsupported active legacy runs.
- [ ] Remove old artifact PUT save implementation; workspace is the authoritative write route. Keep restore/export unrelated functionality.
- [ ] Run focused pipeline/chat/revision tests, correcting assertions for intentionally replaced contracts.

### Task 3: Frontend workspace and removal of inline UI
**Files:** frontend/src/ArtifactWorkspace.tsx, table-review-model.ts, workspaceContext.ts, App.tsx, ArtifactCard.tsx, ConversationParts.tsx, ArtifactActions.tsx and entry components; delete legacy editor/diff files and styles. All frontend ownership belongs to this task.
**Consumes:** Workspace API contract in Global Constraints. `proposal_type` is internal; response uses `artifact_type` and `mode`.
**Produces:** Only the full-screen workspace handles viewing/editing/AI proposal confirmation of cases/scenarios/analysis.
- [ ] Add focused interaction tests for scenario open/edit/add/delete and immutable history; existing case review tests can be updated to new names.
- [ ] Refactor FullScreenReviewer to ArtifactWorkspace, driven by columns/type; preserve per-cell decisions and paired steps. Add row creation/deletion and preserve generic value shapes.
- [ ] Replace all editor and diff call sites with compact workspace entry cards. Route current versions without revision and history with revision/readOnly.
- [ ] AI assistant sends selected row IDs and current artifact bindings, and refreshes proposals into the same grid.
- [ ] Consolidate CSS and remove dead imports/components/tests superseded by new behavior. Preserve unrelated settings/profile actions and exports.
- [ ] Run focused frontend interaction tests and build; report commands/results.

### Task 4: Integration, review and delivery
**Files:** README.md, VERSION/version metadata, RELEASE-MANIFEST.json, release notes; targeted integration fixes assigned back to owning worker.
- [ ] Set up test dependencies without changing application pins, using existing runtime packages or permitted registries.
- [ ] Review backend/frontend contract together; run focused end-to-end/manual/AI/history/conflict flows and production build.
- [ ] Independently review the combined diff and address actionable defects, without repeating already sufficient verification.
- [ ] Update release documentation with migration boundary and test limitations; rebuild distributable frontend.
- [ ] Generate reproducible source zip excluding caches, test data and dependency installations; persist the completed deliverable.
