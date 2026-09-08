# Local intelligent test-design agent

The user approved the full design in the conversation on 2026-09-08. Implement all seven capabilities locally, with no cloud deployment or external database. Preserve existing projects, source evidence, artifact versions, old tasks, SSE replay, request inspection, and 3600-second model timeouts.

## Product behavior

- Default chat uses a new agent experience. Existing API requests that omit `experience` continue to use the legacy graph, including old checkpoints. New UI sends `experience: "agent"`.
- The agent publishes a real model-produced plan and concise evidence-grounded analysis, checks results, can revise its plan and repair uncovered cases, and delivers a substantive summary. Public analysis is an explanation for the user, never private chain of thought or fabricated reasoning progress.
- Design depth: `auto`, `quick`, `standard`, `deep`. Auto is resolved by the model with a reason. These are product presets, not ISTQB certification levels. Techniques include equivalence partitions, boundaries, decision tables and state transitions where applicable. Depth must affect generation context, not just the label.
- Requirement analysis produces a structured business model, rendered as Mermaid, with stable node/edge IDs, evidence references and branch-to-scenario-to-case relationships. A single strategy confirmation pauses before generating cases by default. Blocking business ambiguities pause even when automatic progression was requested. Users can revise the strategy through natural-language feedback.
- The planner chooses only supported actions with checked prerequisites. Explicit iteration/no-progress limits prevent infinite loops. Exhausting a repair budget is a visible incomplete outcome, never a false success or silently reduced coverage.
- Conversation memory preserves confirmed decisions, scope, open questions and source references beyond 12 messages. New conflicting evidence supersedes old assumptions visibly. Memory cannot promote model assumptions to confirmed requirements.
- Running agent tasks accept a durable supplemental instruction, applied at a safe boundary before subsequent work or final publication. Stale results cannot overwrite the changed scope. Existing completed versions remain available; old legacy task behavior is preserved.
- Main UI: chat, attachment, send, one collapsed task-options control. Keep project preferences in settings. Display plans, concise analysis, issues and readable summaries. Raw model JSON and detailed requests remain expandable. New agent runs maintain their SSE subscription while details are collapsed.
- Errors explain stage, impact, preserved work and next actions. Authentication/configuration errors do not retry blindly. Schema repairs are bounded and targeted. Business gaps ask a question. Retry uses checkpoints.
- Model custom headers work in `.env` and settings. Model clients and tests use the same merged configuration; custom-only auth and explicit Authorization are supported. Header values never enter LLM prompts, normal responses, diagnostics, or unredacted request records. UI saved values are encrypted locally. Changing endpoint clears old secrets. Existing API key settings retain backwards compatibility.

## Backend/frontend contract

### Requests

Existing POST `/api/chats/{chat_id}/messages` adds optional fields:
`experience: "legacy" | "agent" = "legacy"`, `depth: "auto" | "quick" | "standard" | "deep" = "auto"`, `confirm_strategy: bool = true`.

POST `/api/runs/{run_id}/instructions` accepts `{content: string}`. Returns `{run: Run}`. Only active agent runs accept this; no second concurrent execution for the chat. Pending instructions are exposed on the run. For waiting strategy/clarification, instruction feedback may be applied by the new graph using its persisted resume mechanism; do not reuse invalid legacy interrupt IDs.

Existing POST `/api/runs/{run_id}/resume` accepts existing `{approved: true}` or `{answer: string}` for strategy/clarification. The frontend uses `/instructions` for strategy edits and `/resume` to approve the updated strategy.

GET `/api/chats/{chat_id}` additionally returns `memory` (public, evidence-grounded summary) when present.

### Run view

New agent runs have `experience: "agent"`, `graph_version: 2`, and:
```
agent: {
  depth: "quick" | "standard" | "deep", rationale: string,
  plan: [{id: string, title: string, status: "pending" | "running" | "completed" | "blocked"}],
  insights: [{id: string, summary: string, refs: string[], kind: "finding" | "decision" | "question" | "summary"}],
  summary?: string,
  coverage?: {requirements_total: number, requirements_covered: number,
    branches_total: number, branches_covered: number, gaps: object[]},
  pending_instructions?: number
}
recovery?: {category: string, title: string, detail: string,
  suggestions: string[], preserved: string[], retryable: boolean}
```
Agent plan/insight updates persist to SQLite and emit `agent` SSE events with `{run_id, agent}`; `update` continues to refresh run snapshots. `model_delta`, `progress`, `done` remain supported unchanged. Errors cannot reveal secret values through exception strings.

Strategy waiting interrupt:
`{type: "strategy_review", artifact_id: string, questions?: string[]}`.
The referenced analysis artifact is visible while waiting. It includes:
```
report: {
  summary?: string, questions?: string[], assumptions?: string[],
  diagrams?: [{id?: string, title: string, mermaid: string}],
  business_model?: {
    nodes: [{id: string, label: string, refs: string[]}],
    edges: [{id: string, from: string, to: string, label: string, refs: string[]}]
  },
  strategy?: {depth: string, rationale: string, techniques: string[], scope: string[]},
  coverage?: {requirements_total: number, requirements_covered: number,
    branches_total: number, branches_covered: number, gaps: object[]}
}
```
Business models and model-generated Mermaid are untrusted. Validate graph references and render Mermaid with strict security, no raw HTML/click directives, length limits and a recoverable error/source view. Coverage means design coverage over the confirmed model, never executed tests or code coverage.

### Model settings

SettingsInput adds `headers?: Record<string,string>`, `clear_headers?: bool = false`, `auth_mode?: "bearer" | "headers" = "bearer"`. Omitted headers preserve saved headers only on the same endpoint. `headers` explicitly replaces the entire set. Keys compare case-insensitively; invalid names, CRLF values and managed transport headers are rejected. Explicit Authorization conflicts with bearer mode: return an actionable validation error requesting headers auth mode. Header-only mode must not send dummy Bearer auth.

Settings public adds `header_names: string[]`, `has_headers: bool`, `auth_mode`. No header values. Model request inspector adds masked `headers: Record<string,string>` only. `.env` adds `TCG_MODEL_HEADERS_JSON` and `TCG_MODEL_AUTH_MODE`; process env overrides file using whole-object replacement, and endpoint changes clear inherited credentials.

## Acceptance

1. Existing Python and frontend regressions remain passing, updated only for intentionally redesigned visible UI.
2. Agent happy path: real LangGraph + scripted model -> plan, grounded business model, strategy pause, approval, scenarios, cases, coverage, grounded completion summary.
3. Invalid graph references, fabricated evidence, no-progress planning, missing coverage and unrecoverable schema errors cannot produce a falsely successful final artifact.
4. Feedback and instructions survive service restart and affect subsequent work; obsolete outputs do not publish. Old graph checkpoints still recover.
5. Env/header persistence, endpoint switching, header-only wire behavior, and secret redaction have regression tests using local HTTP fixtures.
6. Browser checks cover simple composer, strategy diagram, live summary, error guidance, supplemental instruction, settings headers and mobile layout.
7. Build and include frontend/dist so `python3 start.py` remains sufficient. No real remote model billing during tests; document that limitation.
