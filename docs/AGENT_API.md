# Local incremental Agent API (graph version 3)

The web chat explicitly sends `experience: agent`. Python, SQLite, the model adapter,
custom headers and the 3,600-second timeout remain local and unchanged.

## Create work

`POST /api/chats/{chat_id}/messages`:

```json
{
  "content": "根据需求生成测试用例",
  "experience": "agent",
  "depth": "auto",
  "confirm_strategy": false,
  "intent": "auto",
  "mode": "auto"
}
```

Supported intents: `auto`, `review_requirement`, `generate_scenario`, `generate_case`,
`review_case`, `query`, `modify`, `learn_template`. Optional `source_ids`, `profile_id`,
`artifact_id`, `selected_ids`, `as_requirement` retain their existing meanings.
The response is `{message, run}`. New Agent runs have `graph_version: 3`.
Requests omitting `experience` still use the original legacy API default; this is
not the web chat's default. Unfinished v2 Agent checkpoints are not migrated and
receive a clear request to start a new task. Saved sources/artifacts remain usable.

Depth is `auto`, `quick`, `standard`, or `deep`. Auto proceeds with unresolved business
questions recorded as unconfirmed. To review the business direction before generation,
send `mode: hitp, confirm_strategy: true`. Absence of a business subject/source can
still produce an input clarification. Direct business text is classified and saved
verbatim; `as_requirement: true` explicitly saves it without classification.

## Run and work views

`GET /api/runs/{run_id}` returns status, stage, errors/recovery, final `artifact_ids`,
and `agent` with summaries, insights, current depth, `work`, `preview_ids` and
`pause_requested`. `work.items` in live updates is bounded to the latest 20 records.
Total work can grow as pagination and dependencies are discovered; it is not a fixed
percentage of model thinking or an estimate of elapsed time.

`GET /api/runs/{run_id}/work?cursor=0&limit=20` returns:

```json
{
  "completed": 1,
  "total": 2,
  "current": {
    "id": "work_...", "key": "...", "kind": "work_cases",
    "title": "生成用例", "status": "running", "refs": ["src_...#P1"],
    "artifact_id": null, "attempt": 1, "error": null
  },
  "items": [],
  "next_cursor": null
}
```

`cursor >= 0`, `1 <= limit <= 100`. Work records contain no prompt, source body or
private model result. Accepted records may expose a draft `artifact_id`. Status is
`running`, `completed`, `failed`, or `superseded`; superseded work is retained for
inspection but excluded from current completion counts.

## Controls

| Endpoint | Body | Behavior |
| --- | --- | --- |
| `POST /api/runs/{id}/pause` | `{}` | Request pause at the next safe work boundary; does not suspend an upstream request already in flight |
| `POST /api/runs/{id}/resume` | `{"proceed":true}` | Continue a work pause or approved direction |
| `POST /api/runs/{id}/resume` | `{"answer":"业务补充"}` | Answer a clarification or submit direction feedback |
| `POST /api/runs/{id}/instructions` | `{"content":"仅修改登录锁定规则"}` | Persist a versioned instruction and recompute affected work; returns `{run}` |
| `POST /api/runs/{id}/retry` | `{}` | Retry unfinished work after failure; accepted work remains |
| `POST /api/runs/{id}/cancel` | `{}` | Stop the local run, retaining accepted work/history |

Interrupts use `work_pause` (manual boundary or read budget), `strategy_review`, or
`clarification`. A work pause caused by eight read rounds can continue with another
read allowance. Repeated identical tool requests fail visibly instead of looping.
Pure continue commands are not promoted to business evidence. An absent business
subject cannot be bypassed by continue alone.

## SSE and inspection

`GET /api/runs/{id}/events?after={event_id}` supports `Last-Event-ID` and persistent
replay. Existing run/stage/model-call events remain. `agent` carries public Agent
state; `agent_work` carries bounded work progress. Clients deduplicate by event ID
and fetch the paginated inventory only when expanded.

`GET /api/runs/{id}/model-calls/{call_id}/request` returns the recorded request;
`?download=true` downloads it. Header values are redacted. `GET /api/runs/{id}/diagnostics`
adds `runtime` with loaded-at code fingerprint, commit (when available), graph,
protocol, prompt and schema versions. `GET /api/health` exposes the same startup
runtime identity. Live token estimates are labeled as character estimates; provider
usage remains distinct.

## Drafts and final artifacts

`GET /api/artifacts/{id}` returns `preview: true` for an accepted stage draft.
Drafts can be inspected, including evidence and Mermaid graphs, but cannot be edited,
exported, restored or used as a new task's target. Completing the run does not turn
its intermediate drafts into final artifacts. The final published artifact has
`preview: false`, uses the existing revision/export APIs and appears in chat.

Generation publishes after accepted source work, applicable case types and linked
coverage pass validation. Review preserves coverage and edits only the current group.
Explicit selected modifications preserve unselected items. Updates received after
accepted modifications are based on their accepted result and cumulative operations;
publication writes one new revision under the original version guard.

See [implementation and validation boundaries](INCREMENTAL_AGENT.md) for the actual
LangGraph and model-input rules. The retained v2 docs describe historical behavior.
