# TCG v2.6.0 对话控制实施计划

Goal: 在固定生成图外提供统一、可恢复的对话业务调用，让已批准设计中的连续对话真实改变系统。
Architecture: ConversationController + registered capabilities + existing WorkflowEngine/SQLite domain services. 不增加第二个工作流引擎。
Tech Stack: Python/SQLite/LangGraph/FastAPI, React/TypeScript/Vite。
Spec: docs/TCG-CONVERSATIONAL-WORKFLOW-DESIGN.md。

Global constraints: 只读不锁主任务、不因主任务推进失效；明确修改直接保存但不越过确认；预览须明确应用；采用/保存/共享/继续分离；引用、稳定 ID、人工字段、版本受保护；自然语言入口相同；不重复发送整份文档；已有布局和模板策略保持。使用真实数据状态断言及可控模型；不宣称真实内网模型已通过。

## Shared interfaces
- POST /api/chats/{chat_id}/turns: {client_message_id,content,intent_hint?,artifact_id?,artifact_revision?,selected_ids?,view_order?,profile_id?,mode?,source_ids?,reply_to?,command?:{name,arguments}}。
- GET /api/chats/{chat_id}/turns/{turn_id}: persisted TurnResponse.
- Response: {id,client_message_id,status,message,parts:[],pending:[],actions:[]}; persisted assistant message metadata.turn_response holds same renderable result.
- Typed part shapes: answer{text,refs?}; artifact{artifact_id,revision}; case_details{artifact_id,revision,items}; diff{proposal_id,changes}; coverage{data}; files{files:[{name,url}]}; clarification_draft{draft}; estimate{data}.
- Controller resolves artifact_id + selected_ids + expected_revision before domain calls. Model may return bounded {actions:[{name,arguments}],message?}; registered server definitions own effects.
- Domain adapter: async execute(store,engine,chat,name,args,turn_id=None) -> {status:'succeeded|needs_input|needs_confirmation|deferred|failed|cancelled',message,parts:[],pending?:[]}.
- Each adapter may expose CAPABILITIES={name:{effect,description,parameters}}. Root combines registries. Prompts specific to new adapter can register via TASK_INSTRUCTIONS.update at module import; don't concurrently edit model.py.
- Draft: GET /api/runs/{id}/clarification-draft; PATCH same with {expected_revision,answer?,answers?,adopt_ids?,adopt_all?}. Draft includes id,revision,question_set_version,questions:[{id,question,answer,suggestion:{answer,basis,refs,confidence},adopted}],answer,submitted,source_id. All entry points share it.
- Capability names: workflow.start/continue/pause/cancel/retry/update_scope; clarification.adopt/save/share; artifact.read/analyze/estimate/coverage/revise/preview/apply/discard/sync_related/review_cases; project.add_sources/update_from_sources/learn_template/apply_profile/pin_samples; artifact.export.
- Each registered argument schema provides defaults/descriptions; controller validates name, dictionary args and scope; domain validates semantic values.

## Tasks / owners
1. Root: conversation.py controller, conversation_context.py, registry/receipts/turn API integration in main.py; tests/test_conversation_v260.py. Short pending actions, bounded model interpretation, per-action idempotency, plainchat pending resolution, write/control checks.
2. Artifact agent: conversation_artifacts.py and artifact_actions.py changes + domain tests. Pure snapshot reads/analyze/estimate, explicit revision/preview/apply/discard, readonly review, scoped analysis→scenarios→cases sync. Use immutable snapshots, validated operations and existing lineage. Own only these files and own test.
3. Lifecycle agent: conversation_workflow.py, clarification.py, graph.py/workflow.py safe boundary integration, own tests. Persistent shared draft; workflow controls with boundary pause, safe resume and saved-answer reuse. Register draft HTTP routes through helper called by root. Notify root precise queue hooks.
4. Project agent: conversation_project.py + project_context.py and own tests. Learn/apply both templates without generator Run, share profile/sample, frozen export actual files; add source and targeted update adapter coordinating artifacts. New export routes helper called by root. Do not edit main.py/model.py/storage.py.
5. Frontend agent: all frontend/src changes and frontend/tests/conversation-v260.test.tsx. Unified sends and typed parts, persistent draft UI, read requests don't lock confirm, composer available during running, keep layout/prefill; explicit controls call unified commands. No backend changes.
6. Integration agent: inspect/recover runtime dependencies and prepare actual full chain tests/demo fixture documentation. Do not change production modules. Report available commands and honest limitations.

## Verification and completion
- Each owner records focused failure-first and passing behavior tests in docs/superpowers/plans/verification-<owner>.md.
- Root run integration sequence: adopt/edit/reload/save/share; estimate followup; ambiguity; versioned edits and downstream; read while running; duplicate retry; stop supersedes continue; actual export files and templates.
- Frontend DOM integration and TypeScript/production build; capture exact fresh dist list for packaging.
- Independent spec/quality review of changes; fix concrete risks. Bump v2.6.0 and package clean ZIP with integrity hashes, create continuous Chinese demo guide, persist final artifacts.

## Progress
- Isolated release extracted from v2.5.13 ZIP; old release unchanged. User authorizes direct implementation, no further permission gate.

- Completed: unified controller/receipts/context, artifact/project/workflow adapters, persistent drafts, frontend typed turns.
- Reviewed and fixed: late acknowledgments, pending tails, replay/cancel/gate identity, fixed graph boundary nodes, source caches, parent links and immutable results.
- Final focused validation: 84 backend checks,16 new frontend +5 preserved frontend checks, TypeScript/build/compile pass. Full HTTP/LangGraph runtime remains unverified due unavailable dependencies.
- Packaged with continuous Chinese demo and actual template/requirement attachments.
