# Local intelligent design agent API

All state remains in the existing local SQLite data directory. No external database,
background cloud deployment, or additional model service is required. Model calls
use the existing saved/environment connection and the fixed 3600-second timeout.

## Compatibility and goals

`POST /api/chats/{chat_id}/messages` accepts:

```json
{
  "content": "Design authentication tests",
  "experience": "agent",
  "depth": "auto",
  "confirm_strategy": true,
  "intent": "auto",
  "mode": "auto"
}
```

`experience` defaults to `legacy`. Requests that omit it and old checkpoints retain
the original LangGraph. Agent requests use `graph_version: 2`, a separate compiled
LangGraph sharing the durable checkpoint database and exclusive local runner.

All existing goals remain supported: automatic routing, requirement analysis,
scenario generation, case generation, case review/import, grounded questions,
template proposals, and selected Artifact modification. Analysis-only and
scenario-only requests stop at their requested goal. Auto routing uses the existing
bounded metadata-based route contract. Selected modifications retain the original
Artifact ID and create one new revision only after the final publication guard.

With no evidence, a model intake classifier distinguishes concrete user-authored
business requirements from generation instructions and ambiguous messages. Clear
requirements are saved verbatim as an inspectable source; ambiguous or
instruction-only input pauses for clarification. Message length is never the rule.
`as_requirement: true` still explicitly saves the message as a requirement.

## Planning, depth, strategy and coverage

The model produces a plan, depth rationale, evidence-cited public insight, and a
next action chosen from server-checked prerequisites. After analysis it can proceed
or reassess; later it can add targeted scenarios/cases, inspect coverage or finish.
Reassessment invalidates downstream candidates and passes through strategy review
again. Within unchanged user scope it cannot remove confirmed requirement/branch
IDs. Two identical reassessments, repeated unchanged gaps, three repairs, or eighteen
planner iterations stop visibly. Pagination requires new IDs and cursors and has an
explicit 200-page execution budget; exhaustion fails visibly, never truncates success.

Depth values are `auto`, `quick`, `standard`, `deep`. The model resolves `auto` with a
reason. Every generation request includes substantive `depth_guidance`: critical
partitions for quick; positive/negative/boundary coverage for standard; interacting
rules, transition sequences and recovery paths for deep. Every depth must still
cover the confirmed requirements and branches. These are product presets, not
ISTQB certification levels.

The analysis Artifact contains `report.business_model.nodes` and `.edges`, each
with stable IDs, labels and exact non-example evidence refs. Every edge must refer
to existing nodes. The server derives Mermaid from that validated model rather than
executing arbitrary model Mermaid. Source is limited to 30,000 characters and
contains only the generated flowchart syntax and sanitized labels. Frontends must
still use strict Mermaid security and offer a recoverable source view.

`report.strategy` contains `depth`, `rationale`, `techniques`, `scope`. Assumptions
remain separate from grounded requirement items. All supplied business evidence
chunks must be represented in the analyzed requirements. Blocking questions always
pause, including `confirm_strategy: false` requests.

Default strategy pause:

```json
{"type":"strategy_review","artifact_id":"art_...","questions":[]}
```

`POST /api/runs/{run_id}/resume` accepts `{"approved":true}` for strategy and
`{"answer":"..."}` for a clarification interrupt. Strategy feedback uses
`POST /api/runs/{run_id}/instructions` with `{"content":"..."}`. It returns
`{"run":Run}`. After a strategy edit, approve the updated strategy through `/resume`.

Scenarios and cases carry `requirement_ids` and `branch_ids`; cases also carry
`scenario_id`. Case links must be subsets of their parent scenario's links. The
Artifact `report.traceability` contains `id`, `title`, `requirement_ids`,
`branch_ids`, `scenario_id`, `refs`, and `case_id` for cases. Coverage is computed
from accepted links over the confirmed model, never from a model-provided percentage.
Gaps expose `kind`, `id`, readable `title`, and `refs`. Generation cannot finish with
uncovered requirements, branches, or scenarios. A failed repair preserves the
analysis/earlier versions and exposes `recovery.category: "incomplete_coverage"`.
Case/scenario reports retain their requirement/scenario basis. Every AI or manual
revision recomputes coverage and traceability; older reports lacking that basis
lose stale derived fields and expose `coverage_invalidated`. Historical revision
snapshots remain unchanged. Case review also preserves a separate review report.

This is **test-design coverage**, not executed test results or code coverage. Link
validation cannot mathematically establish semantic test completeness: model
analysis, the strategy confirmation, and human review of expected results remain
relevant. Local tests exercise real LangGraph orchestration with scripted models;
they do not establish the quality of an arbitrary remote model.

## Durable instructions and memory

Only active agent runs (`queued`, `running`, `waiting`) accept supplemental
instructions. SQLite stores the verbatim instruction, clarification source,
monotonic instruction version, chat message and confirmed decision. A running model
call may finish, but its obsolete result is rejected before accepting artifacts or
publishing. At the next safe boundary the graph reapplies the instructions and
rechecks strategy. The instruction/call/interrupt boundary and service restarts are
covered by regression tests. A supplemental instruction does not launch another
concurrent run for the chat.

The public chat response adds `memory`, with `decisions`, `scope`, `open_questions`,
`scope_confirmed`, `source_refs`, `assumptions` and `updated_at`. Open questions are
saved before the task pauses. Confirmed decisions are derived from
explicit strategy approvals or verbatim user clarification/instructions. They are
not limited to the last twelve messages. Automatic strategy choices and model
assumptions are not promoted to confirmed decisions. Analysis can report a conflict
using an existing decision ID and newer change/clarification evidence; the old
decision then remains visible with `status: "superseded"`, `superseded_by` refs and a
public supersession insight.

## Run snapshots, events and recovery

Agent runs expose:

```json
{
  "experience":"agent",
  "graph_version":2,
  "agent":{
    "depth":"standard",
    "rationale":"...",
    "plan":[{"id":"analyze","title":"Analyze requirements","status":"running"}],
    "insights":[{"id":"ins_...","summary":"...","refs":["src_...#P1"],"kind":"finding"}],
    "pending_instructions":0,
    "coverage":{"requirements_total":1,"requirements_covered":1,"branches_total":2,"branches_covered":2,"gaps":[]},
    "summary":"..."
  }
}
```

Plans and public insights persist and emit `agent` SSE events with `{run_id,agent}`.
Existing `update`, `model_delta`, `progress`, `done`, event IDs and SSE replay remain
available. Public insights explain findings and decisions; they are not private
chain of thought. Expandable model output and request inspection remain available.

Failures add `recovery: {category,title,detail,suggestions,preserved,retryable}`.
Authentication/configuration failures do not receive blind automatic retries.
Structured outputs receive one targeted schema-repair attempt. `/retry` uses the
checkpoint and clears the failed raw call cache while retaining earlier accepted
work. Retry does not erase explicit loop/coverage budgets; unresolved business scope
may require cancelling and starting a narrower task. Provider exception contents,
request validation inputs and secret values are never included in error messages.

## Custom headers and authentication

Settings input adds:

```json
{"headers":{"X-Api-Key":"value"},"clear_headers":false,"auth_mode":"headers"}
```

Public settings return only `header_names`, `has_headers`, and `auth_mode`, alongside
existing settings. Header values are encrypted locally with the existing Fernet key.
Omitted headers retain the saved set only on the same provider/endpoint. Explicit
`headers` replaces the entire set; `{}` or `clear_headers: true` clears it. Endpoint
changes clear inherited API keys and headers. Existing API-key inputs remain valid.

Header names are case-insensitive, unique HTTP tokens; at most 32 are accepted.
Values must be single-line ASCII and cannot contain controls/CRLF. Managed transport
headers such as Host, Content-Length, Connection, Transfer-Encoding, Upgrade and
Trailer are rejected. An explicit Authorization header requires `auth_mode: "headers"`.
Header-only OpenAI requests remove the SDK's default Bearer header at the actual
HTTP request boundary and then apply the explicit headers. Ollama receives the same
merged custom/auth configuration, with ambient `OLLAMA_API_KEY` stripped at the
wire boundary. Both providers disable redirects so arbitrary custom secrets cannot
be forwarded to another origin; configure the final model endpoint directly.
Connection tests use that same gateway.

Environment variables:

```dotenv
TCG_MODEL_AUTH_MODE=headers
TCG_MODEL_HEADERS_JSON='{"X-Api-Key":"your-value"}'
```

Process environment overrides the file using whole-object replacement. Changing an
endpoint in a higher-priority layer discards lower-priority credentials. Environment
managed settings remain read-only in the settings UI. The model request inspector
includes header names with masked values only; header values never enter prompts,
ordinary settings responses, diagnostics or persisted unredacted request records.

Local HTTP fixtures verify actual OpenAI/Ollama wire headers, header-only mode,
explicit Authorization, masking, endpoint clearing, encryption and nonretryable
authentication errors. No tests call or bill a remote model API.
