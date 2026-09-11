# Task 1 implementation report

Implemented the common commit boundary in `operations.py`; existing Store creation/revision APIs delegate to it. Revisions and dependency rows are inserted in the same SQLite transaction as the current artifact, audit, receipt and confirmation refresh. No model calls occur here.

## APIs

- `Store.artifact(..., *, command_id=None, dependencies=None)` preserves positional arguments. Its default receipt key is the Run ID plus cache key.
- `Store.revise_artifact(..., *, source_ids=None, source_roles=None, command_id=None, dependencies=None)` atomically enriches sources and roles while validating the expected revision, optional consumed manifest, project boundary and Run cancellation. Source enrichment is additive. Existing cache keys receive stable receipt IDs.
- `Store.annotate_artifact(artifact_id, expected_revision, fields, reason='report_update', run_id=None)` accepts report/title fields and returns the complete new artifact revision. Historical report payloads never change.
- `operations.cancel_command(store, command_id)` persistently rejects an uncommitted command. A previously committed receipt always returns its original complete result, even after a subsequent edit or cancellation.
- `dependencies.manifest(store, artifact_ids=(), source_ids=(), profile_ids=(), run_id=None)` produces a versioned canonical digest with explicit artifact revisions, source versions/content+chunk digests, immutable profile snapshots and optional effective Run input scope. Artifact references may be IDs or `{id, revision}` dictionaries; multiple historical versions are retained.
- `dependencies.assert_manifest(store, value)` rejects changed consumed inputs with `DomainError(409)`. Control version/stop policy are excluded from business input freshness. Request scope, live Run scope, profile, sources and instruction/input versions are included.
- `dependencies.record_artifact`, `artifact_status`, and `impact` expose exact version/item lineage and `current`, `needs_review`, `stale`. Partial synchronization advances only the specified parent rows. Impact returns direct affected rows and conservative same-chat candidates; `partial=True` explicitly disclaims complete semantic impact coverage.

Current artifact business fields cannot be rewritten at an existing revision or rolled back to an older head through `Store.put`. Visibility and existing operational `_agent_target` metadata remain compatible. SQLite triggers also forbid historical revision UPDATE/DELETE. Source updates preserve earlier payloads and content digests in `input_versions`; profile versions preserve complete snapshots there.

The consumed self baseline is checked but excluded from persisted upstream dependencies. Linked parent artifacts use relation row precision for status, avoiding false whole-artifact staleness after a different parent row changes.

## Verification

- A real Store instantiated from baseline commit `49c24b2` demonstrated that a historical revision SQL rewrite succeeded and the annotation API was absent.
- `PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python -m pytest -q tests/test_framework_storage_v270.py`: **8 passed**. Tests cover immutable reports/current rows, two scenarios and two corresponding cases, source/scope/profile invalidation, control-only stability, exact receipt replay, cancellation, atomic enrichment, historical manifests, self-dependency exclusion and partial relation synchronization.
- After the root-owned `artifact_actions.py` atomic-enrichment replacement, the focused storage plus legacy workspace suite passed: **21 passed**. Earlier broader conversation collection had 30 passes plus six pytest collection errors for unittest helper functions lacking fixture `data`; these are outside Task 1 ownership.

## Limits and integration notes

Callers must capture and pass a consumed manifest before model computation for full source/profile freshness checking; legacy calls without one still validate target revision and Run state and persist source provenance. Stable command IDs are required for retry receipts outside Run cache-backed operations. Existing legacy artifact relations are interpreted read-only when no relation row exists. Source historical payloads preserve complete source text; this change does not create a separate historical chunk retrieval API. Field-selection and protected-manual-field policy remain with the typed operation callers. The common commit calls root-owned `workflow_bindings.refresh_confirmation` inside its transaction for fresh revisions only.
