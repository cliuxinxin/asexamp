# TCG Runnable System Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development with scoped ownership and review.

**Goal:** Deliver a real runnable TCG 2.2 system using the approved analysis.
**Architecture:** Fixed LangGraph workflow with durable SQLite work-unit results and direct HTTP gateway.
**Tech Stack:** FastAPI, LangGraph, SQLite, HTTPX, Pydantic, React, native document parsers.
**Spec:** docs/superpowers/specs/2026-09-09-runnable-system.md

## Global Constraints
- Python 3.11+; prebuilt frontend; .env backend only; no fake production model.
- Single-process durable recovery; explicit phase progression; bounded repairs; no silent source loss.
- Existing API and export conventions retained; new UI uses experience=reliable.

### Task 1: Private gateway
Files: backend/tcg/model.py, backend/tcg/environment.py, tests/test_gateway_v22.py, .env.example.
Interface: retain LangChainGateway.generate(task, context) and generate_stream(task, context, on_text).
- [x] Add HTTP fixture tests verifying endpoint, X-API-Key, content arrays and no undocumented parameters.
- [x] Run `.venv/bin/python -m pytest tests/test_gateway_v22.py -q` and verify red.
- [x] Implement direct HTTPX openai-provider path; preserve Ollama path; configure timeout honestly.
- [x] Verify nonretryable auth/protocol errors, empty/truncated/invalid JSON rejection and credential redaction.
- [x] Run focused gateway tests; report changes and tests for review.

### Task 2: Fixed workflow and durable work items
Files: backend/tcg/reliable.py, backend/tcg/graph.py, backend/tcg/schemas.py, backend/tcg/storage.py,
backend/tcg/main.py, tests/test_reliable_workflow.py.
Interface: ReliableEngine subclasses Engine, graph_version=4; run.progress={phase,completed,total,label}.
- [x] Write integration tests selecting experience=reliable, long documents, invalid references and retry.
- [x] Run tests before implementation and record expected failure.
- [x] Add context partitioning and source-unit ledger; override fixed pipeline nodes for version 4.
- [x] Persist accepted work-unit output and page cursors, prevent duplicate IDs and no-progress pagination.
- [x] Implement batched correction with all reference errors and preservation checks; bounded repairs.
- [x] Implement explicit memory API and immutable case_types/depth snapshots.
- [x] Verify full generation and XLSX export, HITP and process recovery.

### Task 3: Frontend and document intake
Files: frontend/src/App.tsx, frontend/src/RunCard.tsx, frontend/src/types.ts,
frontend/src/SourcesDialog.tsx, frontend/src/styles.css, backend/tcg/documents.py.
Interfaces: experience=reliable; case_types string array; memory API under /projects/{id}/memory;
progress counts in Run; source.classification={label,reason,provisional}.
- [x] Add frontend interaction assertions for pre-run types and progress.
- [x] Add reliable mode selection, visible depth/types, source role correction and project memory controls.
- [x] Increase explicit document limits; optional Docling scan parser; configurable error messages.
- [x] Build frontend and verify interaction components plus HTTP static serving; browser visual inspection blocked by client and explicitly recorded.

### Task 4: Packaging and final verification
Files: start.py, requirements.txt, Dockerfile, compose.yaml, README.md, docs/VALIDATION_V22.md.
- [x] Scope decision: retain loopback-only launcher and origin protection for local demonstration; remote multiuser deployment deferred.
- [x] Pin versions actually installed and tested; separate optional heavy parsing dependencies.
- [x] Run relevant existing regression suite and new tests; fix material regressions.
- [x] Perform HTTP-backed end-to-end test using test-only fixture; independently review final changes.
- [x] Package source and built frontend excluding credentials/data/environments; verify archive contents.
- [x] Save ZIP and report exact startup commands and private endpoint validation limitation (final delivery step).
