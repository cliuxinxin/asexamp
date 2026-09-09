"""Same-origin FastAPI service, local static frontend and dependency injection."""
import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlparse, quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .diagnostics import endpoint_origin, error_details
from .documents import MAX_UPLOAD, classify_source, export_cases, parse_document, parse_text
from .environment import runtime_value
from .workflow import WorkflowEngine as Engine
from .model import LangChainGateway, Settings
from .schemas import ChatInput, DomainError, MessageInput, NameInput, ProfileInput, RestoreInput, ResumeInput, RevisionInput, ROLES, SettingsInput, TextInput
from .storage import DirectoryLock, Store, public, uid, now

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_public(value):
    return {key: item for key, item in public(value).items() if item is not None}


def message_public(value):
    return {key: value[key] for key in ('id', 'role', 'content', 'created_at', 'metadata')}


class WaitingEditInput(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    selected_ids: list[str] | None = None


class MemoryInput(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    kind: str = 'preference'


class SourceRoleInput(BaseModel):
    role: str


def create_app(data_dir: Path | str | None = None, model_gateway=None):
    directory = Path(data_dir or os.environ.get('TCG_DATA_DIR', PROJECT_ROOT / 'data')).expanduser().resolve()

    @asynccontextmanager
    async def lifespan(app):
        with DirectoryLock(directory):
            store = Store(directory)
            engine = None
            try:
                settings = Settings(directory)
                gateway = model_gateway if model_gateway is not None else LangChainGateway(settings)
                engine = Engine(store, gateway, settings)
                app.state.store, app.state.settings, app.state.engine = store, settings, engine
                await engine.start()
                yield
            finally:
                if engine:
                    await engine.stop()
                if 'gateway' in locals() and hasattr(gateway, 'close'):
                    await gateway.close()
                store.close()

    app = FastAPI(title='TCG Case Agent Local', version='2.5.4', lifespan=lifespan)

    def run_view(value):
        result = run_public(value)
        result['diagnostic'] = app.state.engine.diagnostics.latest(value['id'])
        return result

    @app.middleware('http')
    async def request_logging(request: Request, call_next):
        request_id = uid('req_')
        started = time.monotonic()
        log = getattr(getattr(app.state, 'engine', None), 'diagnostics', None)
        route = lambda: getattr(request.scope.get('route'), 'path', '/unmatched')
        try:
            with log.bind(request_id=request_id) if log else nullcontext():
                response = await call_next(request)
        except Exception as exc:
            if log:
                log.record('http.error', level='ERROR', request_id=request_id, method=request.method, route=route(), **error_details(exc))
            raise
        response.headers['X-Request-ID'] = request_id
        if log:
            level = 'WARNING' if response.status_code >= 400 else 'DEBUG' if request.method in ('GET', 'HEAD') else 'INFO'
            log.record('http.response', level=level, request_id=request_id, method=request.method, route=route(), status=response.status_code, elapsed_ms=round((time.monotonic() - started) * 1000))
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({'detail': exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Validation input can contain credentials; never echo submitted values.
        return JSONResponse({'detail': [{'loc': e['loc'], 'type': e['type'], 'msg': e['msg']} for e in exc.errors()]}, status_code=422)

    @app.middleware('http')
    async def local_origin(request: Request, call_next):
        host = request.headers.get('host', '')
        hostname = urlparse('//' + host).hostname
        if hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
            return JSONResponse({'detail': '仅允许本地回环地址访问'}, status_code=403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin is not None:
                parsed = urlparse(origin)
                if parsed.netloc != host or parsed.scheme != request.url.scheme:
                    return JSONResponse({'detail': '拒绝跨站修改请求；请从本应用页面操作'}, status_code=403)
        return await call_next(request)

    def configured():
        return model_gateway is not None or app.state.settings.configured()

    def visible_artifact(artifact_id):
        artifact = app.state.store.get('artifact', artifact_id)
        if not artifact.get('_visible'):
            raise DomainError('未找到已发布的 Artifact', 404)
        return artifact

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'version': '2.5.4', 'storage': 'local', 'model_configured': configured()}

    @app.get('/api/projects/{project_id}/memory')
    def memory_list(project_id: str):
        app.state.store.get('project', project_id)
        return [public(m) for m in app.state.store.list('memory', project_id=project_id) if m.get('active', True)]

    @app.post('/api/projects/{project_id}/memory')
    def memory_add(project_id: str, body: MemoryInput):
        store = app.state.store
        store.get('project', project_id)
        if body.kind not in ('preference', 'business') or not body.content.strip():
            raise DomainError('请选择偏好或已确认业务规则，并填写内容')
        with store.transaction():
            previous = memory_list(project_id)
            if len(previous) >= 50:
                raise DomainError('项目确认记忆最多50条，请删除过期规则')
            if sum(len(m['content']) for m in previous) + len(body.content) > 8000:
                raise DomainError('项目确认记忆超过8000字符，请编辑或删除过期规则')
            return store.put('memory', {'id': uid('mem_'), 'project_id': project_id, 'content': body.content.strip(),
                                       'kind': body.kind, 'active': True, 'created_at': now()})

    @app.delete('/api/projects/{project_id}/memory/{memory_id}')
    def memory_delete(project_id: str, memory_id: str):
        store = app.state.store
        with store.transaction():
            item = store.get('memory', memory_id)
            if item['project_id'] != project_id:
                raise DomainError('记忆不属于当前项目', 404)
            return store.put('memory', {**item, 'active': False})

    @app.put('/api/sources/{source_id}/role')
    def source_role(source_id: str, body: SourceRoleInput):
        if body.role not in ROLES:
            raise DomainError('来源角色无效')
        store = app.state.store
        with store.transaction():
            source = store.get('source', source_id)
            if store.runs(chat_id=source['chat_id'], statuses=('queued', 'running', 'waiting')):
                raise DomainError('请在运行结束后修改来源角色；已有任务保留原分类', 409)
            return public(store.put('source', {**source, 'role': body.role,
                'classification': {'label': body.role, 'reason': '用户已确认', 'provisional': False}}))

    @app.get('/api/settings')
    def settings_get():
        return app.state.settings.public()

    @app.put('/api/settings')
    def settings_put(body: SettingsInput):
        return app.state.settings.save(body.model_dump())

    @app.post('/api/settings/test')
    async def settings_test():
        if not configured():
            return {'ok': False, 'message': '请先填写并保存模型名称与服务地址。'}
        log = app.state.engine.diagnostics
        settings = app.state.settings.value
        log.record('connection_test.start', provider=settings['provider'], model=settings['model'], endpoint=endpoint_origin(settings['base_url']), timeout_seconds=settings['timeout_seconds'])
        started = time.monotonic()
        try:
            gateway = app.state.engine.gateway
            if hasattr(gateway, 'test'):
                await asyncio.wait_for(gateway.test(), timeout=app.state.settings.value['timeout_seconds'])
            else:
                await asyncio.wait_for(gateway.generate('connection_test', {}), timeout=app.state.settings.value['timeout_seconds'])
            log.record('connection_test.complete', elapsed_ms=round((time.monotonic() - started) * 1000))
            return {'ok': True, 'message': '模型服务连接成功。'}
        except asyncio.TimeoutError as exc:
            log.record('connection_test.error', level='ERROR', **error_details(exc))
            return {'ok': False, 'message': '连接超时，请确认模型服务已启动。'}
        except Exception as exc:
            log.record('connection_test.error', level='ERROR', **error_details(exc))
            return {'ok': False, 'message': str(exc) if isinstance(exc, DomainError) else '模型服务连接失败，请检查地址和模型名称。'}

    @app.get('/api/projects')
    def projects_get():
        return app.state.store.list('project')

    @app.post('/api/projects')
    def projects_post(body: NameInput):
        return app.state.store.create_project(body.name)

    @app.get('/api/projects/{project_id}/profiles')
    def profiles_get(project_id: str):
        app.state.store.get('project', project_id)
        return app.state.store.list('profile', project_id=project_id)

    @app.post('/api/projects/{project_id}/profiles')
    def profiles_post(project_id: str, body: ProfileInput):
        return app.state.store.create_profile(project_id, body.name, body.config)

    @app.put('/api/profiles/{profile_id}')
    def profiles_put(profile_id: str, body: ProfileInput):
        if body.expected_version is None:
            raise DomainError('保存 Profile 需要 expected_version')
        return app.state.store.update_profile(profile_id, body.name, body.config, body.expected_version)

    @app.get('/api/projects/{project_id}/chats')
    def chats_get(project_id: str):
        app.state.store.get('project', project_id)
        return sorted(app.state.store.list('chat', project_id=project_id), key=lambda chat: chat['updated_at'], reverse=True)

    @app.post('/api/projects/{project_id}/chats')
    def chats_post(project_id: str, body: ChatInput):
        return app.state.store.create_chat(project_id, body.title)

    @app.get('/api/chats/{chat_id}')
    def chat_get(chat_id: str):
        store = app.state.store
        chat = store.get('chat', chat_id)
        return {'chat': chat, 'memory': chat.get('memory'), 'messages': [message_public(m) for m in sorted(store.list('message', chat_id=chat_id), key=lambda m: m['created_at'])], 'sources': [public(s) for s in store.list('source', chat_id=chat_id) if s['_active']], 'runs': [run_view(r) for r in store.runs(chat_id=chat_id)]}

    @app.post('/api/chats/{chat_id}/sources')
    async def sources_upload(chat_id: str, file: UploadFile = File(...), role: str = Form('auto')):
        store = app.state.store
        store.get('chat', chat_id)
        if role not in ROLES and role != 'auto':
            raise DomainError('来源 role 无效')
        try:
            max_mb = int(runtime_value(directory, 'TCG_MAX_UPLOAD_MB', '100'))
            if not 1 <= max_mb <= 500:
                raise ValueError()
        except ValueError:
            raise DomainError('TCG_MAX_UPLOAD_MB 必须在1至500之间') from None
        limit = max_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise DomainError(f'文件超过 {max_mb} MB 限制', 413)
        name = Path((file.filename or 'document.txt').replace('\\', '/')).name[:200]
        digest = hashlib.sha256(data).hexdigest()
        existing = next((s for s in store.list('source', chat_id=chat_id) if s.get('_active') and s.get('_sha256') == digest
                         and (role == 'auto' or s['role'] == role)), None)
        if existing:
            return public(existing)
        started = time.monotonic()
        app.state.engine.diagnostics.record('source.parse_start', chat_id=chat_id, file_type=Path(name).suffix.lower(), bytes=len(data))
        parser = runtime_value(directory, 'TCG_DOCUMENT_PARSER', 'native')
        if parser not in ('native', 'docling'):
            raise DomainError('TCG_DOCUMENT_PARSER 必须为native或docling')
        text, chunks = await asyncio.to_thread(parse_document, name, data, limit, parser)
        if role == 'auto':
            role, classification = classify_source(name, text)
        else:
            classification = {'label': role, 'reason': '用户指定', 'provisional': False}
        app.state.engine.diagnostics.record('source.parse_complete', chat_id=chat_id, chunks=len(chunks), characters=len(text), elapsed_ms=round((time.monotonic() - started) * 1000))
        source_id = uid('src_')
        upload_dir = directory / 'uploads'
        upload_dir.mkdir(exist_ok=True)
        path = upload_dir / (source_id + Path(name).suffix.lower())
        await asyncio.to_thread(path.write_bytes, data)
        try:
            with store.transaction():
                existing = next((s for s in store.list('source', chat_id=chat_id) if s.get('_active') and s.get('_sha256') == digest and s['role'] == role), None)
                if existing:
                    path.unlink(missing_ok=True)
                    return public(existing)
                source = store.add_source(chat_id, name, role, text, chunks, str(path.relative_to(directory)), source_id)
                source = store.put('source', {**source, '_sha256': digest, 'classification': classification})
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return public(source)

    @app.post('/api/chats/{chat_id}/sources/text')
    def sources_text(chat_id: str, body: TextInput):
        text, chunks = parse_text(body.text)
        return public(app.state.store.add_source(chat_id, body.name, body.role, text, chunks))

    @app.get('/api/sources/{source_id}')
    def source_get(source_id: str):
        store = app.state.store
        source = store.get('source', source_id)
        return {**public(source), 'text': source['_text'], 'chunks': [{key: e[key] for key in ('id', 'text', 'location')} for e in store.evidence([source_id])]}

    @app.delete('/api/sources/{source_id}')
    def source_delete(source_id: str):
        app.state.store.deactivate_source(source_id)
        return {'ok': True}

    @app.post('/api/chats/{chat_id}/messages')
    async def messages_post(chat_id: str, body: MessageInput):
        if not configured():
            raise DomainError('尚未配置模型。请在设置中填写本地 Ollama 模型名称，或兼容 API 服务。')
        store = app.state.store
        store.get('chat', chat_id)
        request = body.model_dump()
        with store.transaction():
            # Evidence promotion is an explicit user action, never an inference
            # from a message's length or generation-related vocabulary.
            if body.as_requirement:
                text, chunks = parse_text(body.content)
                source = store.add_source(chat_id, '消息中的需求正文', 'primary', text, chunks)
                if request['source_ids'] is not None:
                    request['source_ids'] = [*request['source_ids'], source['id']]
            message, run = store.create_run(chat_id, request)
        app.state.engine.schedule(run['id'])
        return {'message': message_public(message), 'run': run_view(run)}

    @app.get('/api/runs/{run_id}')
    def run_get(run_id: str):
        return run_view(app.state.store.run(run_id))

    @app.get('/api/runs/{run_id}/diagnostics')
    def run_diagnostics(run_id: str, download: bool = False):
        engine, store = app.state.engine, app.state.store
        run = store.run(run_id)
        history = len(run.get('_conversation', []))
        payload = {
            'version': '2.5.4', 'run_id': run_id, 'chat_id': run['chat_id'],
            'error':run.get('error'),'failed_node':run.get('failed_node'),'failed_stage':run.get('failed_stage'),'validation_errors':run.get('validation_errors',[]),
            'status': run['status'], 'stage': run['stage'], 'created_at': run['created_at'],
            'updated_at': run['updated_at'],
            'runtime': {'graph_thread_id': run_id, 'task_active': engine.task_active(run_id), 'diagnostic_storage_degraded': engine.diagnostics.storage_degraded, 'diagnostic_file_degraded': engine.diagnostics.file_degraded},
            'context': {'conversation_messages': history, 'history_limit': 12,
                        'history_omitted': max(0, run.get('_history_total', history) - history),
                        'source_count': len(run.get('_source_ids', [])),
                        'artifact_id': (run.get('_artifact_snapshot') or {}).get('id')},
            'events': engine.diagnostics.rows(run_id),
            'retention': 'Last 200 diagnostic events; request/response content is not included.',
        }
        headers = {'Content-Disposition': f'attachment; filename="{run_id}-diagnostics.json"'} if download else {}
        return JSONResponse(payload, headers=headers)

    @app.get('/api/runs/{run_id}/failed-step')
    def run_failed_step(run_id: str, call_id: str | None = None):
        from .failure_report import failed_step_report
        report=failed_step_report(app.state.store,app.state.engine.diagnostics,run_id,call_id)
        return Response(report,media_type='text/markdown; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{run_id}-failed-step.md"'})

    @app.get('/api/runs/{run_id}/debug-bundle')
    async def run_debug_bundle(run_id: str):
        import io,zipfile
        store=app.state.store;run=store.run(run_id)
        graph=app.state.engine.workflow if run.get('graph_version')==7 else app.state.engine.graph
        checkpoint=await graph.aget_state(app.state.engine.config(run_id))
        metadata=json.loads(run_diagnostics(run_id).body)
        metadata['events']=app.state.engine.diagnostics.rows(run_id,500)
        metadata['retention']='Last 500 diagnostic events; all stored request and response snapshots for this run are attached.'
        metadata['checkpoint']={'next':list(checkpoint.next),'values':checkpoint.values,
            'interrupts':[{'id':i.id,'value':i.value} for i in checkpoint.interrupts]}
        metadata['profile_snapshot']=run['_profile']
        with store.lock:
            calls=[dict(row) for row in store.db.execute('SELECT call_id,payload FROM model_requests WHERE run_id=?',(run_id,)).fetchall()]
            outputs=[dict(row) for row in store.db.execute('SELECT call_id,content FROM model_outputs WHERE run_id=?',(run_id,)).fetchall()]
            cache_keys=[row['key'] for row in store.db.execute('SELECT key FROM cache WHERE run_id=?',(run_id,)).fetchall()]
        metadata['cache_keys']=cache_keys
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('diagnostics.json',json.dumps(metadata,ensure_ascii=False,indent=2))
            for row in calls:archive.writestr('calls/'+row['call_id']+'.request.json',row['payload'])
            for row in outputs:archive.writestr('calls/'+row['call_id']+'.response.txt',row['content'])
            archive.writestr('artifacts.json',json.dumps([public(a) for a in store.list('artifact',chat_id=run['chat_id']) if a['id'] in set(run['artifact_ids'])|{v for k,v in checkpoint.values.items() if k.endswith('_ref')}],ensure_ascii=False,indent=2))
        return Response(buffer.getvalue(),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}-debug.zip"'})

    @app.post('/api/runs/{run_id}/resume')
    async def run_resume(run_id: str, body: ResumeInput):
        return run_view(app.state.engine.resume(run_id, body.model_dump()))

    @app.post('/api/runs/{run_id}/dialogue')
    async def run_dialogue(run_id: str, body: WaitingEditInput):
        return await app.state.engine.paused_dialogue(run_id, body.content)

    @app.post('/api/runs/{run_id}/instructions')
    async def run_instruction(run_id: str, body: WaitingEditInput):
        return {'run': run_view(app.state.engine.agent.add_instruction(run_id, body.content))}

    @app.post('/api/runs/{run_id}/retry')
    async def run_retry(run_id: str):
        if not configured():
            raise DomainError('请先配置模型再重试')
        return run_view(app.state.engine.retry(run_id))

    @app.post('/api/runs/{run_id}/cancel')
    async def run_cancel(run_id: str):
        return run_view(app.state.engine.cancel(run_id))

    @app.post('/api/runs/{run_id}/edit')
    async def run_edit(run_id: str, body: WaitingEditInput):
        if not body.content.strip():
            raise DomainError('编辑指令不能为空')
        return run_view(await app.state.engine.edit_waiting(run_id, body.content, body.selected_ids))

    @app.get('/api/runs/{run_id}/model-calls/{call_id}/request')
    def model_request_get(run_id: str, call_id: str, download: bool = False):
        payload = app.state.store.model_request(run_id, call_id)
        headers = {'Cache-Control': 'no-store'}
        if download:
            headers['Content-Disposition'] = f'attachment; filename="{call_id}-request.json"'
        return JSONResponse(payload, headers=headers)

    @app.get('/api/runs/{run_id}/events')
    async def run_events(run_id: str, request: Request):
        store = app.state.store
        store.run(run_id)
        try:
            after = max(0, min(2**63 - 1, int(request.headers.get('last-event-id') or request.query_params.get('after', '0'))))
        except ValueError:
            after = 0

        async def stream():
            cursor = after
            yield 'retry: 1500\n\n'
            while True:
                events = store.events(run_id, cursor)
                for event in events:
                    cursor = event['id']
                    yield f'id: {cursor}\nevent: {event["kind"]}\ndata: {json.dumps(event["data"], ensure_ascii=False)}\n\n'
                current = store.run(run_id)
                if current['status'] in ('completed', 'failed', 'cancelled') and not app.state.engine.task_active(run_id) and not store.events(run_id, cursor):
                    yield f'event: done\ndata: {json.dumps(run_view(current), ensure_ascii=False)}\n\n'
                    return
                if await request.is_disconnected():
                    return
                if len(events) == 200:
                    continue
                yield ': keepalive\n\n'
                await asyncio.sleep(.5)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.get('/api/artifacts/{artifact_id}')
    def artifact_get(artifact_id: str):
        return public(visible_artifact(artifact_id))

    @app.put('/api/artifacts/{artifact_id}')
    def artifact_put(artifact_id: str, body: RevisionInput):
        visible_artifact(artifact_id)
        return public(app.state.store.revise_artifact(artifact_id, body.expected_revision, body.items, report=body.report))

    @app.get('/api/artifacts/{artifact_id}/revisions')
    def artifact_revisions(artifact_id: str):
        visible_artifact(artifact_id)
        return app.state.store.revisions(artifact_id)

    @app.get('/api/artifacts/{artifact_id}/revisions/{revision}')
    def artifact_revision(artifact_id: str, revision: int):
        visible_artifact(artifact_id)
        return public(app.state.store.revision(artifact_id, revision))

    @app.post('/api/artifacts/{artifact_id}/restore')
    def artifact_restore(artifact_id: str, body: RestoreInput):
        visible_artifact(artifact_id)
        historical = app.state.store.revision(artifact_id, body.revision)
        return public(app.state.store.revise_artifact(artifact_id, body.expected_revision, historical['items'], reason=f'restore:{body.revision}'))

    @app.get('/api/artifacts/{artifact_id}/export-options')
    def artifact_export_options(artifact_id: str):
        artifact = visible_artifact(artifact_id)
        return {'snapshot': artifact.get('_profile',{}), 'profiles': app.state.store.list('profile',project_id=artifact['project_id'])}

    @app.get('/api/artifacts/{artifact_id}/export')
    def artifact_export(artifact_id: str, layout: str | None = None, ids: str | None = None, profile_id: str | None = None):
        artifact = visible_artifact(artifact_id)
        if profile_id:
            profile = app.state.store.get('profile',profile_id)
            if profile['project_id'] != artifact['project_id']:
                raise DomainError('导出 Profile 不属于当前项目')
            artifact = {**artifact, '_profile':profile['config']}
        layout = layout or artifact.get('_profile', {}).get('excel_layout', 'case')
        output = export_cases(artifact, layout, ids.split(',') if ids is not None else None)
        project = app.state.store.get('project', artifact['project_id'])
        pattern = artifact.get('_profile',{}).get('filename_pattern','{project}_{date}.xlsx')
        filename = str(pattern).replace('{project}',project['name']).replace('{date}',now()[:10])
        filename = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in filename)[:160]
        if not filename.endswith('.xlsx'): filename += '.xlsx'
        return Response(output, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': "attachment; filename=\"test-cases.xlsx\"; filename*=UTF-8''" + quote(filename)})

    @app.get('/{path:path}', include_in_schema=False)
    def frontend(path: str):
        if path.startswith('api/'):
            raise DomainError('API 路由不存在', 404)
        root = (PROJECT_ROOT / 'frontend' / 'dist').resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise DomainError('路径无效', 404)
        if candidate.is_file():
            return FileResponse(candidate)
        if Path(path).suffix:
            raise DomainError('文件不存在', 404)
        index = root / 'index.html'
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({'detail': '前端尚未构建。请在 frontend 目录运行 npm install 和 npm run build。'}, status_code=503)

    return app


app = create_app()
