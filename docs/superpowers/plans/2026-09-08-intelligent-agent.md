# Intelligent Local Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Deliver the approved intelligent local test-design experience, retaining existing local data and workflows.

**Architecture:** Version the graph per task. Keep the legacy engine for legacy checkpoints; add a bounded evidence-grounded agent graph for new UI requests. Reuse local storage, model gateway and persistent SSE events, with focused frontend components for plan, diagrams, errors and secret header settings.

**Tech Stack:** Python, FastAPI, LangGraph, LangChain, SQLite, React, TypeScript, locally bundled Mermaid.

**Spec:** docs/superpowers/specs/2026-09-08-intelligent-agent.md

## Global Constraints

- No cloud deployment; model timeouts stay 3600 seconds.
- Preserve old checkpoints, artifacts, IDs, evidence and exports.
- Credentials never enter prompts, diagnostics or ordinary API responses.
- Only one active execution per chat; pending instructions must be durable.
- All backend/frontend JSON interfaces use the exact names in the spec.
- English code comments, Chinese user-facing text.

## Task 1: Versioned backend agent and model connectivity

**Files:** backend/tcg/{agent.py,agent_contracts.py,graph.py,storage.py,main.py,model.py,schemas.py,environment.py}; new tests/test_agent_*.py and tests/test_custom_headers.py; backend documentation docs/AGENT_API.md.

**Interfaces:** consumes existing Store, Engine, gateway, checkpoints. Produces all requests, run.agent, recovery, report.business_model, report.strategy, masked headers and SSE contracts in the spec.

- [ ] Add behavioral tests before production edits. For example:
```python
run = start(client, chat, experience='agent', confirm_strategy=True)
waiting = until(client, run)
assert waiting['interrupt']['type'] == 'strategy_review'
assert waiting['agent']['plan']
assert client.post('/api/runs/'+run['id']+'/resume', json={'approved': True}).status_code == 200
final = until(client, run)
assert final['status'] == 'completed'
assert final['agent']['coverage']['requirements_total'] > 0
```
- [ ] Run the tests and observe missing agent experience behavior, then implement the versioned graph, bounded plans and coverage-driven targeted fixes.
- [ ] Add durable instructions, strategy feedback, confirmed memory and truthful summaries. Test instructions during a blocked local model call and across app restart.
- [ ] Add header configuration, encrypted saving, masking, shared connection tests and local HTTP wire tests. Verify headers-only authentication and endpoint changes.
- [ ] Run focused tests, then the Python suite. Commit only backend-owned files and tests.
- [ ] Review spec compliance and backend correctness independently; fix material issues.

## Task 2: Friendly chat, plans, business diagrams and model settings

**Files:** frontend/src/{App.tsx,types.ts,SettingsDialog.tsx,RunCard.tsx,RunTimeline.tsx,ArtifactCard.tsx,AgentPanel.tsx,BusinessDiagram.tsx,AnalysisReport.tsx,styles.css}; frontend tests and package files.

**Interfaces:** consumes Task 1 contract, retaining legacy run rendering. New UI sends experience='agent', depth and confirm_strategy.

- [ ] Write failing DOM tests for collapsed options, agent SSE summaries, strategy approval, supplemental instructions and masked header controls.
- [ ] Implement frontend components and contextual task options. For agent updates use persisted `agent` event data, keep stream subscription while details are collapsed, and preserve numeric event cursor deduplication.
- [ ] Lazy load Mermaid, enforce strict sanitized rendering, use recoverable syntax errors, and offer full-screen/SVG/source. Present report fields as readable content with JSON tucked away.
- [ ] Render recovery actions, grounded insights and final summaries. Keep keyboard accessibility, mobile layout, project isolation, draft behavior and all existing editing/export paths.
- [ ] Run DOM tests and TypeScript/build, then browser acceptance using a local scripted model and actual backend.
- [ ] Review frontend changes independently and fix material issues.

## Task 3: Integration, documentation and GitHub delivery

**Files:** README.md, docs/AGENT_GUIDE.md, .env.example, .gitignore, frontend/dist.

- [ ] Verify old and new paths together: backend suite, frontend suite, build and browser checks.
- [ ] Document depth presets, custom headers, strategy feedback, public reasoning summaries, coverage limits, versioning and recovery.
- [ ] Include rebuilt frontend assets. Inspect diff for credentials, accidental data files and deployment code.
- [ ] Commit, push a feature branch through available GitHub tools, and open a reviewable PR targeting main.

## Progress

- Plan approved by user through "就这样实施"; execute without another approval checkpoint.
