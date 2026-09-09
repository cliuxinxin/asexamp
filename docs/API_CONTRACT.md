> 本文保留自 v2.1 基线，包含旧 Agent 和旧超时行为。v2.2 的启动、能力与限制以根目录 README.md 为准。

# Chat application API contract

All JSON UTF-8. FastAPI errors return `{detail: string}` (or standard validation detail). Datetimes ISO8601. IDs strings. REST responses below are unwrapped unless stated. Local single-user, no account setup. Mutation routes reject Origin from another host; no broad CORS. Production static app served same port. Dev server proxies /api to Python.

## Setup and resources

`GET /api/health` -> `{status:"ok", version:"2.0.7", storage:"local", model_configured:boolean}`

`GET /api/settings` -> `{provider:"ollama"|"openai",base_url:string,model:string,has_api_key:boolean,timeout_seconds:number}`. Default ollama http://127.0.0.1:11434, model empty until selected. No secret returned.

`PUT /api/settings` accepts provider,base_url,model,optional api_key,optional clear_api_key. Preserve secret only if same provider/base_url and omitted. Secret encrypted with local key file (0600). API key not needed for Ollama or some local compatible endpoints.

`POST /api/settings/test` -> `{ok:boolean,message:string}` tests SAVED model config, errors actionable, no secret disclosure. Only explicit invocation calls model.

`GET /api/projects` -> project[]; each `{id,name,created_at}`. Create default project on initialization.
`POST /api/projects {name}` -> project.

`GET /api/projects/{id}/profiles` -> profile[] each `{id,project_id,name,version,config}`. Config default: `{language:"中文",scenario_level:"standard",case_level:"standard",case_types:["Business","Negative","Boundary"],additional_rules:"",scope:"",excel_layout:"case",sheet_name:"Test Cases"}` plus schema/knowledge options as implemented.
`POST /api/projects/{id}/profiles {name,config}` -> profile. `PUT /api/profiles/{id} {name,config,expected_version}` -> profile (409 on stale).

`GET /api/projects/{id}/chats` -> chat[]; `{id,project_id,title,created_at,updated_at}` newest first.
`POST /api/projects/{id}/chats {title?:string}` -> chat.
`GET /api/chats/{id}` -> `{chat,messages:Message[],sources:Source[],runs:Run[]}`.

Message `{id,role:"user"|"assistant",content:string,created_at,metadata:object}`.
Metadata can contain `artifact_ids:string[]`, `run_id`, `proposal` (profile config), `refs:string[]`. Models must never output arbitrary HTML. Frontend renders plain text.

Source `{id,project_id,chat_id,name,role,characters,created_at}` roles primary,change,supplement,clarification,example,knowledge.
`POST /api/chats/{id}/sources` multipart `file`, `role` default primary -> Source. Allow max15MB DOCX, PDF(text), XLSX, XLS, CSV, TXT, MD; retain originals locally. Files never resolve arbitrary user paths.
`POST /api/chats/{id}/sources/text {name,text,role}` -> Source.
`GET /api/sources/{id}` -> Source plus `text` and `chunks:[{id,text,location}]`.
`DELETE /api/sources/{id}` marks inactive for future runs, preserves historical evidence.

## Runs and conversation

`POST /api/chats/{id}/messages {content, as_requirement?:boolean, intent:"auto"|"review_requirement"|"generate_scenario"|"generate_case"|"review_case"|"query"|"learn_template"|"modify", mode:"auto"|"hitp", profile_id?:string, source_ids?:string[], artifact_id?:string, selected_ids?:string[]}` -> `{message,run}`. Empty content invalid. Missing source_ids uses active chat sources (plus selected profile knowledge if implemented). Only explicit as_requirement=true promotes the current message to a primary source. Ordinary instructions never become evidence based on length or keywords. Generation without source evidence returns an actionable missing-input response. `artifact_id` scopes review/modify. Enforce same-project references. Prior conversation and current artifact considered in routing. No model config -> HTTP 400 before starting run, user content not silently lost in UI.

Run `{id,chat_id,project_id,status:"queued"|"running"|"waiting"|"completed"|"failed"|"cancelled",intent,mode,stage,error?:string,created_at,updated_at,artifact_ids:string[],interrupt?:{type:"clarification"|"scenario_review",questions?:string[],artifact_id?:string,items?:object[]}}`.

`GET /api/runs/{id}` -> Run plus optional `diagnostic` (latest safe event, or null for runs without new diagnostics) (do not expose private checkpoint/intermediates in Auto).
`GET /api/runs/{id}/events?after=0` -> persisted SSE: `update`, `progress`, `model_delta`, followed by `done` only after terminal task activity ends and all pending events drain. Details below. The stream stays open during waiting. No browser request is required to advance the graph.
`POST /api/runs/{id}/resume {answer?:string,approved?:boolean}` -> Run. Resume real LangGraph interrupt. Scenario edits done through artifact route before approval; graph consumes latest version. Must have valid answer for clarification; approved true for scenario review.
`POST /api/runs/{id}/retry {}` -> Run. Failed node resumes checkpoint, not whole run reset; source/profile snapshot preserved. May use repaired model config.
`POST /api/runs/{id}/cancel {}` -> Run.

`POST /api/runs/{id}/edit {content,selected_ids?:string[]}` -> Run. Only waiting scenario_review. Apply a model-proposed patch to the latest scenario revision while preserving the paused run. A durable edit token and revision guard reject concurrent/late changes after resume or cancellation.

## Artifacts

`GET /api/artifacts/{id}` -> `{id,chat_id,project_id,type:"analysis"|"scenarios"|"cases"|"review"|"answer"|"proposal",title,revision:number,items:object[],report?:object,created_at}`.
Stable item IDs, evidence `refs:string[]`. Common fields:
- Scenario `{id,title,description,priority,refs}`.
- Case `{id,title,scenario_id,type,priority,preconditions,steps:[{action,expected}],refs}`.
- Analysis `{id,title,description,refs}`; report optional structured global map, assumptions, questions, diagrams.
Additional fields permitted and preserved. Frontend generic JSON editor alongside readable case editor supports arbitrary fields.

`PUT /api/artifacts/{id} {expected_revision,items}` -> Artifact, validate duplicates/refs and schemas; 409 stale. Atomic immutable revision + pointer update and audit.
`GET /api/artifacts/{id}/revisions` -> `[{revision,created_at,reason,diff}]`.
`GET /api/artifacts/{id}/revisions/{revision}` -> Artifact snapshot.
`POST /api/artifacts/{id}/restore {revision,expected_revision}` -> Artifact (new revision).
`GET /api/artifacts/{id}/export?layout=case|step&ids=id1,id2` -> real XLSX. Only cases export. Omitted ids = all, specified ids = selected; validate ids. Formula escaping. Fixed headers Case ID,Title,Type,Priority,Preconditions,Steps,Expected Result; no audit/profile rows. Custom column mapping is not implemented.

## Backend integration interface

Expose `backend/tcg/main.py:create_app(data_dir: Path|str|None=None, model_gateway=None)` returning FastAPI. `app = create_app()` for Uvicorn.
Data directory default from TCG_DATA_DIR or project_root/data. Root start.py runs `uvicorn.run("tcg.main:app",host="127.0.0.1",port=8000,workers=1)` with backend on sys.path. Static root project_root/frontend/dist.

Injectable model gateway method `async generate(task: str, context: dict) -> dict` used in graph and tests. If provider test needs separate method `async test()` document it. `task` names and structured contracts documented in backend code; production implementation uses LangChain. Test model never selectable in user-facing settings or environment.

Tests use pytest and FastAPI TestClient with lifespan (or explicit app lifespan for ASGITransport), temp data dirs, fake model injection. Browser integration harness can create_app(temp_path, fake_model) separately outside distributed runtime entrypoint.

## v2.0.1 diagnostics

`GET /api/runs/{id}/diagnostics?download=true` returns safe task metadata, runtime `{graph_thread_id,task_active,diagnostic_storage_degraded,diagnostic_file_degraded}`, context `{conversation_messages,history_limit:12,history_omitted,source_count,artifact_id}`, and up to 200 recent diagnostic events. `download=true` adds Content-Disposition. No request/response bodies, source text, credentials or full exception messages are included. Each run retains up to 500 diagnostic rows. INFO application logs also write to local `logs/tcg.log`; slow model calls emit a heartbeat every 10 seconds. Every HTTP response handled by the request logging middleware has X-Request-ID. Normal GET access noise is disabled by start.py unless requested.

## v2.0.2 routing input

Auto routing uses an independently constructed context capped at 12,000 serialized JSON characters. It includes a request preview capped at 4,000 serialized characters, at most 4 prior message previews capped at 800 each, source role/count/file-name metadata, and current artifact type/revision/item count. Evidence bodies, artifact items/reports, arbitrary message metadata and Profile free text are excluded from this classification call. Long previews retain both ends and explicitly mark omissions; source documents, stored messages and actual analysis input remain intact. Explicit intents skip the model routing request. Failed previous-version routing runs can retry against the existing checkpoint. Diagnostic error metadata now distinguishes application_status, provider_http_status and http_status_source, retaining legacy http_status.


## v2.0.3 review validation and correction

The review node validates add/update/delete operations and the resulting full case set before revision commit. Known scenario IDs are checked for generated cases. No field values are coerced and no invalid review is silently accepted. Case string fields are explicitly specified in the review task contract. Schema/operation validation failures permit at most one additional corrective response per execution; transport calls retain their existing per-request timeout and maximum one transport retry. A valid first review incurs no correction call.

Correction uses the same review task with `validation_repair: {validation_error, previous_response}` and the original cases/evidence. The previous response stays in the existing local model cache and is sent to the already configured model for correction; it is never included in diagnostics. Corrected operations apply once to the original snapshot. The accepted response/report and resulting revision commit in one SQLite transaction. A failed correction preserves the original draft and checkpoint; manual retry resumes review. Auto intermediate drafts remain unpublished until the task completes.

Events: `review.validation_failed`, `review.repair_started`, `review.repair_complete`. Validation-related error metadata adds `validation_error: {code,path,expected,actual}`, for example `{code:"type_mismatch",path:"items[0].preconditions",expected:"string",actual:"array"}`. Paths use zero-based indices and fixed schema field names; values, raw item IDs and response bodies are excluded. Cancellation/terminal state is checked before each transport retry to prevent another model call after cancellation. No database migration is required.


## v2.0.4 environment configuration and case page correction

Settings response adds `environment_managed: boolean` and `env_file: string|null` alongside existing fields. These contain only safe metadata; `has_api_key` reflects the effective credential. When environment-managed, PUT /api/settings returns 409; POST /api/settings/test tests the current effective settings without saving. Otherwise existing encrypted persistence remains available. The UI preserves timeout_seconds on save.

`TCG_MODEL_PROVIDER`, `TCG_MODEL_BASE_URL`, `TCG_MODEL_NAME`, `TCG_API_KEY`, `TCG_MODEL_TIMEOUT_SECONDS` map to model settings. Settings load one env file (explicit TCG_ENV_FILE, otherwise data/.env, otherwise project/.env); process variables override file fields, which override saved settings. Endpoint changes discard lower-priority credentials unless the same layer supplies a key. An explicit empty key suppresses any saved key. Any model env entry enables environment-managed mode. Config reload requires restart, no raw key is copied to settings.json. The stdlib parser supports single-line assignments, optional export, quotes and comments, no shell execution or variable interpolation. The launcher resolves --env-file and passes TCG_ENV_FILE to the actual backend process.

Each generated/imported case page now permits one correction on OutputValidationError, including original response and safe validation_error in validation_repair. Task prompts require action and expected together in every step object. Invalid pages never enter cases_valid caches. Corrections must retain page item count, valid unique IDs in order, existing valid sibling case content, has_more and any already-valid continuation cursor; invalid IDs/cursors can be repaired. Rejected corrections cannot silently remove coverage. Events cases.validation_failed / cases.repair_started / cases.repair_complete include page_index. Retries use the existing graph checkpoint and page cursor. Pagination progress errors retain the existing visible failure behavior. Transport timeout/retry budgets remain unchanged.


## v2.0.5 streaming progress

The SSE endpoint responds with Content-Type text/event-stream, Cache-Control no-cache, X-Accel-Buffering no. It begins with retry:1500 and emits keepalive comments while idle. If reverse-proxying, disable proxy response buffering and allow long-lived connections.

| Event | Data | Persistence |
|---|---|---|
| update | Run snapshot or minimal run_id/stage/status | Monotonic SQLite event id |
| progress | Safe diagnostic metadata: event, at, run_id, node, stage, call_id, elapsed_ms, batch_index/count, attempt, validation_error as applicable | Same event sequence |
| model_delta | {at,run_id,call_id,text}; text fragments up to 2048 characters | Same event sequence; contains model response body |
| done | Public Run | No separate id; signals fully drained terminal stream |

An SSE id is global across runs, monotonically increasing but not contiguous for one run. Last-Event-ID takes precedence over the after query parameter. Only ids greater than the cursor replay. Empty or invalid cursors default to zero; numeric cursors are clamped to SQLite integer range. Batches of 200 drain completely, including terminal histories longer than 200. done waits for both graph and paused-edit tasks to finish at completed/failed/cancelled. Waiting remains live, enabling paused edits to stream over the same connection.

Frontend EventSource automatically reconnects with Last-Event-ID, with a manual reconnect button for a permanently closed connection. UI effect recreation passes its saved cursor as after; new run IDs reset it. Duplicate IDs and stale callbacks are ignored. UI updates are batched for 80ms; incoming update events debounce chat refresh, and a 7-second state query is a backup. Completed histories load only when expanded.

Production gateway adds async generate_stream(task,context,on_text) using actual ChatOpenAI/ChatOllama astream. on_text receives only visible content, never separate reasoning/thinking fields. AIMessageChunk aggregation retains final finish metadata for truncation checks and JSON parsing. Transport and total attempt timeout budgets remain enforced. The small connection test uses non-streaming generate; it does not verify streaming capability. Injected test adapters with only generate remain supported: a complete JSON response is emitted once and model.start contains streaming:false.

Each actual invocation has a unique call_id. The model.start event creates the output card; model.complete means response parsing completed, while node.complete means the stage passed its checks. Validation failures and repair requests remain distinct. model_delta does not create or revise an Artifact; partial/invalid JSON is never published as a successful result. Cancelled calls cannot append late model text or commit late revisions. Paused edit lifecycle uses node=paused_edit; cancellation terminates its tracked task and returns HTTP 409. Diagnostic runtime.task_active now covers graph and paused edits.

Migration adds kind TEXT NOT NULL DEFAULT 'update' to existing events tables; old rows remain readable. Progress uses the safe diagnostic metadata path, but raw deltas bypass Diagnostics, rotating files and downloads. Full SSE history has no automatic retention limit; it contains local business data. Existing diagnostic row limits remain 500 stored / 200 downloaded. Older model fragments cannot be reconstructed. Restart preserves already stored text but may reissue interrupted model requests under a new call_id.


## v2.0.6 hour timeout policy and request inspection

Settings adds timeout_policy:"fixed_60_minutes"; effective timeout_seconds is always 3600 for normal runtime configuration. Valid legacy env/process/API values 5..3600 normalize to 3600; missing values also default to 3600. Existing settings.json is normalized in memory on load. Other environment precedence and key isolation rules remain unchanged. Original env files are not rewritten. Settings PUT writes 3600 even for an old UI payload containing 120/300. The UI shows this policy and disables timeout editing. Invalid env values remain explicit configuration errors.

The shared effective setting governs Engine's total per-call asyncio deadline, connection tests, ChatOpenAI's HTTP timeout, and ChatOllama's sync/async HTTP client timeout. This sets HTTP connect/read/write/pool waits to 3600 as well. Automatic retries remain at most one and each gets its own deadline; the whole workflow can exceed 60 minutes. SSE heartbeat/reconnect intervals, SQLite lock waits, startup health probes, and test harness deadlines are not model request timeouts and are unchanged. A provider or reverse proxy may still impose a shorter deadline.

GET /api/runs/{run_id}/model-calls/{call_id}/request?download=false returns a saved snapshot or 404 if either scope has no matching record. Response has Cache-Control:no-store. download=true adds attachment filename {call_id}-request.json.

Snapshot fields: {at,run_id,call_id,node,call_key,attempt,task,provider,base_url,model,timeout_seconds,messages:[{role,content}],parameters:{temperature,stream,format?|response_format?},representation:"langchain_messages_and_explicit_parameters"}. messages are the same message objects' contents passed to the real LangChain ainvoke/astream call, serialized immediately before invocation. parameters include only the explicit application options; SDK-added wire fields are not represented. Snapshot means prepared for sending, not proof of remote receipt. Headers, API key, ciphertext and HTTP client objects are never serialized. Message bodies are preserved verbatim; user-authored business text is not redacted or reconstructed.

CREATE TABLE IF NOT EXISTS model_requests(run_id,call_id,payload,PRIMARY KEY(run_id,call_id)) adds persistent, append-only snapshots. Storage lookup always includes both IDs. Request bodies never enter diagnostics or general SSE payloads. Safe progress event model.request_saved carries call_id/request_available:true/message_count; the frontend enables the inspection button and fetches the full snapshot only when opened. The modal provides system prompt, parsed context and full record tabs plus download, renders plain text, and aborts/ignores stale reads after switching calls or closing.

Request capture is installed only on the production gateway, using the active diagnostic context solely to associate run/call IDs. Injected test gateways without a recorder do not fabricate inputs. Small settings connection tests have no run/call association and are not stored. Old calls without snapshots return 404 and show unavailable; historical inputs cannot be backfilled. Capture storage failure fails the invocation before the network call rather than silently sending an unauditable request. Successful snapshots and safe progress events remain available across failure/retry/restart. Full input snapshots can contain local business data and currently have no automatic retention limit.


## v2.0.7 bootstrap recovery

start.py checks the local interpreter, pyvenv.cfg, activate script and a working pip CLI. Missing skeleton files trigger EnvBuilder(with_pip=False) without clearing the directory; missing pip triggers a separate visible python -u -m ensurepip --upgrade --default-pip -v process. Repaired environments rerun the locked requirements installation before continuing. Bootstrap KeyboardInterrupt exits 130 with a restart instruction; failed child commands retain their exit code and visible error output. --no-install continues to use the current interpreter. Model timeouts, request inspection and API behavior are unchanged except the reported version.
