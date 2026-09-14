"""The generation pipeline: pointer state, native interrupts and durable checkpoints.

The chat agent is a client of this runtime. It never edits a shadow copy of the
pipeline's control state. SQLite stores business artifacts; the checkpointer is
the authority for which node is waiting and what can resume it.
"""
import asyncio
import copy
from contextlib import asynccontextmanager, nullcontext, suppress
from contextvars import ContextVar
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .schemas import DomainError
from .storage import public, now
from .model_diagnostics import failure_part


class PipelineState(TypedDict, total=False):
    run_id: str
    project_id: str
    chat_id: str
    analysis_ref: str
    scenario_ref: str
    cases_ref: str
    review_ref: str
    current_artifact_id: str
    phase: str


GATES = {
    'understanding_gate': ('strategy_review', 'analysis_ref', '确认需求理解', 'scenarios'),
    'scenario_gate': ('scenario_review', 'scenario_ref', '确认测试场景', 'cases'),
    'case_draft_gate': ('case_draft_review', 'cases_ref', '确认用例草稿', 'review'),
    'review_result_gate': ('case_result_review', 'cases_ref', '确认评审建议', 'apply_review'),
}
MESSAGES = {
    'strategy_review': '请检查需求理解。回复“同意”继续，也可以直接提问、修改或补充资料。',
    'scenario_review': '请检查场景及关联需求。回复“同意”继续，也可以直接说明修改意见。',
    'case_draft_review': '请检查用例步骤、预期和关联场景。回复“同意”开始评审，也可以直接微调。',
    'case_result_review': '用例尚未按评审修改。请查看评审建议与修改预览；回复“同意”后应用建议，也可以直接补充评审意见。',
}
ENTRY_PREDECESSORS = {'direct_cases': 'understanding_gate', 'scenarios': 'understanding_gate', 'cases': 'scenario_gate', 'review': 'case_draft_gate'}


class PipelineRuntime:
    def __init__(self, store, business, checkpoint_path=None):
        self.store, self.business = store, business
        self.checkpoint_path = checkpoint_path or store.directory / 'pipeline-checkpoints.sqlite3'
        self.graph = None
        self.tasks = {}
        self._locks = {}
        self._chat_locks = {}
        self._mutation_owner = ContextVar('pipeline_mutation_owner', default=None)
        self._saver_context = None
        self._stopping = False

    def _config(self, run_id):
        return {'configurable': {'thread_id': run_id}, 'recursion_limit': 40}

    def _lock(self, run_id):
        # Only serialize actual execution in this process, never a paused graph.
        return self._locks.setdefault(run_id, asyncio.Lock())

    @asynccontextmanager
    async def _mutation_session(self, chat_id):
        owner = (chat_id, asyncio.current_task())
        if self._mutation_owner.get() == owner:
            yield
            return
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            token = self._mutation_owner.set(owner)
            try:
                yield
            finally:
                self._mutation_owner.reset(token)

    async def ensure_editable(self, chat_id):
        if self.store.runs(chat_id=chat_id, statuses=('queued', 'running')):
            raise DomainError('当前步骤正在生成；可以先提问，或要求暂停后修改', 409)

    @asynccontextmanager
    async def edit_session(self, chat_id):
        async with self._mutation_session(chat_id):
            await self.ensure_editable(chat_id)
            yield

    async def start(self):
        self._stopping = False
        self._saver_context = AsyncSqliteSaver.from_conn_string(str(self.checkpoint_path))
        saver = await self._saver_context.__aenter__()
        await saver.setup()
        graph = StateGraph(PipelineState)
        for name in ('understand', 'clarification_gate', 'apply_clarification',
                     'understanding_gate', 'scenarios', 'scenario_gate', 'cases',
                     'direct_cases', 'case_draft_gate', 'review', 'review_result_gate', 'apply_review', 'finish'):
            graph.add_node(name, getattr(self, '_node_' + name))
        graph.add_edge(START, 'understand')
        graph.add_edge('understand', 'clarification_gate')
        graph.add_conditional_edges('clarification_gate', lambda s:
                                    'apply_clarification' if s['phase'] == 'clarification_answered' else 'understanding_gate')
        graph.add_conditional_edges('apply_clarification', lambda s:
            'clarification_gate' if self._questions(self._artifact(s, 'analysis_ref')) else 'understanding_gate')
        graph.add_conditional_edges('understanding_gate', self._after_understanding,
            {'finish': 'finish', 'scenarios': 'scenarios', 'skip_to_cases': 'direct_cases'})
        graph.add_edge('scenarios', 'scenario_gate')
        graph.add_conditional_edges('scenario_gate', lambda s: self._after(s, 'scenarios', 'cases'))
        graph.add_edge('cases', 'case_draft_gate')
        graph.add_edge('direct_cases', 'case_draft_gate')
        graph.add_conditional_edges('case_draft_gate', lambda s: self._after(s, 'cases', 'review'))
        graph.add_edge('review', 'review_result_gate')
        graph.add_conditional_edges('review_result_gate', lambda s:
            'finish' if s.get('phase') == 'review_rejected' else 'apply_review')
        graph.add_edge('apply_review', 'finish')
        graph.add_edge('finish', END)
        self.graph = graph.compile(checkpointer=saver)
        for run in self.store.runs(statuses=('queued', 'running', 'waiting')):
            if run.get('runtime') != 'native':
                continue
            state = await self.graph.aget_state(self._config(run['id']))
            if state.tasks and any(t.interrupts for t in state.tasks):
                await self.snapshot(run['id'])
            elif run['status'] in ('queued', 'running'):
                self._schedule(run['id'], None if state.values else self._initial(run))

    async def stop(self):
        self._stopping = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        if self._saver_context:
            await self._saver_context.__aexit__(None, None, None)
            self._saver_context = None
        self.graph = None

    def _initial(self, run):
        start = run.get('_request', {}).get('_pipeline_start') or {}
        return {'run_id': run['id'], 'chat_id': run['chat_id'], 'project_id': run['project_id'],
                'phase': start.get('stage', 'understand'), **start.get('refs', {})}

    def _existing_entry(self, chat_id, request):
        """Resolve an explicit start artifact; never infer or approve an active gate."""
        artifact_id = request.get('artifact_id')
        if not artifact_id:
            if request['intent'] == 'review_case':
                raise DomainError('请明确指定需要评审的已有用例成果')
            return None
        chat = self.store.get('chat', chat_id)

        def artifact(aid, kind=None):
            value = self.store.get('artifact', aid)
            if value['chat_id'] != chat_id or value['project_id'] != chat['project_id'] or not value.get('_visible'):
                raise DomainError('起步成果必须是当前对话中可查看的成果')
            if kind and value['type'] != kind:
                raise DomainError('起步成果的上游关联类型不正确')
            from .conversation_facts import ensure_artifact_allowed
            ensure_artifact_allowed(self.store, value)
            return value

        current = artifact(artifact_id)
        supported = {('analysis', 'generate_scenario'): 'scenarios',
                     ('analysis', 'generate_case'): 'scenarios',
                     ('scenarios', 'generate_case'): 'cases',
                     ('cases', 'review_case'): 'review'}
        stage = supported.get((current['type'], request['intent']))
        if request.get('skip_scenarios'):
            if current['type'] != 'analysis':
                raise DomainError('跳过场景需要从需求理解开始，请指定需求成果')
            if self._questions(current):
                raise DomainError('请先回答当前需求中的澄清问题，再跳过场景生成用例', 409)
            stage = 'direct_cases'
        if not stage:
            raise DomainError('这份成果不能用于所请求的生成起点；请说明要从需求生成场景、从场景生成用例，还是评审已有用例')
        selected = request.get('selected_ids')
        if selected is not None:
            all_ids = {item['id'] for item in current['items']}
            if not isinstance(selected, list) or not selected or not all(isinstance(i, str) for i in selected) or not set(selected) <= all_ids:
                raise DomainError('请选择起步成果中有效的条目；评审全部时请不要传入空的选择范围')
            if stage != 'review' and set(selected) != all_ids:
                raise DomainError('从已有需求或场景继续生成目前需要完整成果；请先明确生成范围，系统不会扩大所选条目')
        stop_after = request.get('stop_after')
        positions = {'analysis': 0, 'scenarios': 1, 'cases': 2, 'direct_cases': 2, 'review': 3}
        if stop_after in positions and positions[stop_after] < positions[stage]:
            raise DomainError('停止位置早于所请求的工作，请明确要继续到哪个阶段')
        key = {'analysis': 'analysis_ref', 'scenarios': 'scenario_ref', 'cases': 'cases_ref'}[current['type']]
        refs = {key: current['id'], 'current_artifact_id': current['id']}
        lineage = (current.get('report') or {}).get('lineage') or {}
        if current['type'] in ('scenarios', 'cases'):
            scenario = current if current['type'] == 'scenarios' else None
            if lineage.get('scenario_artifact_id'):
                scenario = artifact(lineage['scenario_artifact_id'], 'scenarios')
                refs['scenario_ref'] = scenario['id']
            analysis_id = lineage.get('analysis_artifact_id') or (((scenario or {}).get('report') or {}).get('lineage') or {}).get('analysis_artifact_id')
            if analysis_id:
                refs['analysis_ref'] = artifact(analysis_id, 'analysis')['id']
            if stage == 'cases' and 'analysis_ref' not in refs:
                raise DomainError('这份场景缺少明确关联的需求理解，请先绑定对应需求，再继续生成用例')
        return {'stage': stage, 'refs': refs, 'artifact_revision': current['revision']}

    async def start_run(self, chat_id, request):
        async with self._mutation_session(chat_id):
            return await self._start_run(chat_id, request)

    async def _start_run(self, chat_id, request, *, schedule=True):
        request = {**request, 'experience': 'native',
                   'intent': request.get('intent', 'generate_case'),
                   'mode': request.get('mode', 'hitp'),
                   'content': request.get('content', request.get('requirements', '生成测试用例'))}
        request.pop('_pipeline_start', None)
        if request.get('skip_scenarios') and (request['intent'] != 'generate_case' or
                request.get('stop_after', 'review') not in ('cases', 'review')):
            raise DomainError('跳过场景仅适用于生成测试用例或用例草稿')
        entry = self._existing_entry(chat_id, request)
        if entry:
            request['_pipeline_start'] = entry
        _, run = self.store.create_run(chat_id, request)
        stop_after = request.get('stop_after')
        if not stop_after:
            stop_after = {'review_requirement': 'analysis', 'generate_scenario': 'scenarios'}.get(request['intent'], 'review')
        with self.store.transaction():
            inherited = [self.store.get('artifact', aid) for aid in dict.fromkeys(entry['refs'].values())] if entry else []
            inherited_sources = [sid for value in inherited for sid in value.get('_source_ids', [])]
            inherited_roles = {sid: role for value in inherited for sid, role in value.get('_source_roles', {}).items()}
            run = self.store.update_run(run['id'], runtime='native', graph_version=10, skip_scenarios=bool(request.get('skip_scenarios')),
                stop_after=stop_after, pause_after_step=bool(request.get('_knowledge_rebuild')),
                artifact_ids=[a['id'] for a in inherited],
                _source_ids=list(dict.fromkeys(run['_source_ids'] + inherited_sources)),
                _source_roles={**run.get('_source_roles', {}), **inherited_roles},
                start_context=({'artifact_id': request['artifact_id'], 'artifact_revision': entry['artifact_revision'],
                                'next_stage': entry['stage'], 'behavior': 'review_current_cases' if entry['stage'] == 'review' else 'generate_downstream'} if entry else None))
        if schedule:
            self._schedule(run['id'], self._initial(run))
        return public(run)

    def _schedule(self, run_id, value):
        if self._stopping:
            raise DomainError('服务正在停止，请稍后重试', 409)
        if run_id in self.tasks and not self.tasks[run_id].done():
            raise DomainError('当前步骤仍在执行，请等待完成', 409)
        task = asyncio.create_task(self._execute(run_id, value), name='pipeline:' + run_id)
        self.tasks[run_id] = task
        task.add_done_callback(lambda completed: self.tasks.pop(run_id, None)
                               if self.tasks.get(run_id) is completed else None)

    async def _execute(self, run_id, value):
        diagnostics = getattr(self, 'diagnostics', None)
        run = self.store.run(run_id)
        binding = diagnostics.bind(run_id=run_id, chat_id=run['chat_id'], project_id=run['project_id']) if diagnostics else nullcontext()
        with binding:
            await self._execute_graph(run_id, value)

    async def _execute_graph(self, run_id, value):
        async with self._lock(run_id):
            if self.store.run(run_id)['status'] == 'cancelled':
                return
            self.store.update_run(run_id, status='running', error=None, interrupt=None,
                                  interrupt_id=None, failed_node=None, candidate_id=None,
                                  failure_category=None, failure_call_id=None, repair_progress=None)
            try:
                if isinstance(value, dict) and value.get('phase') in ENTRY_PREDECESSORS:
                    await self.graph.aupdate_state(self._config(run_id), value,
                                                  as_node=ENTRY_PREDECESSORS[value['phase']])
                    value = None
                await self.graph.ainvoke(value, self._config(run_id))
                await self.snapshot(run_id)
            except asyncio.CancelledError:
                # Shutdown leaves the last committed node resumable on restart.
                raise
            except Exception as exc:
                diagnostic = failure_part(exc)
                diagnostics = getattr(self, 'diagnostics', None)
                if diagnostics:
                    from .diagnostics import error_details
                    self._record('pipeline.failed', run_id, level='ERROR',
                                 node=self.store.run(run_id).get('stage'), call_id=diagnostic['call_id'],
                                 reference_id=diagnostic['reference_id'], category=diagnostic['category'],
                                 **error_details(exc))
                with self.store.transaction():
                    if self.store.run(run_id)['status'] != 'cancelled':
                        run = self.store.run(run_id)
                        detail = str(exc) if isinstance(exc, DomainError) else '处理遇到异常，请按诊断编号查看服务日志。'
                        self.store.update_run(run_id, status='failed', error=detail,
                                              failed_node=run.get('stage'), interrupt=None,
                                              interrupt_id=None, repair_progress=None,
                                              failure_category=diagnostic['category'], failure_call_id=diagnostic['call_id'])
                        from .generation_candidates import save_candidate, STAGE_LABELS
                        candidate = save_candidate(self.store, run, exc)
                        parts = [diagnostic]
                        stage_label = STAGE_LABELS.get(run.get('stage'), '当前步骤')
                        if candidate:
                            parts.insert(0, candidate)
                            self.store.update_run(run_id, candidate_id=candidate['candidate_id'])
                            message = stage_label + '已自动修复 3 次，仍有未通过的校验。生成内容已保留为待核对草稿，可打开查看具体问题和各次输出。你可以补充说明，或回复“重试当前步骤”。'
                        else:
                            message = stage_label + '未完成：' + detail + ' 已有成果已保留；可按下方排查信息处理，或回复“重试当前步骤”。'
                        message_id = 'pipeline-error:' + diagnostic['reference_id']
                        self.store.put('message', {'id': message_id, 'project_id': run['project_id'],
                            'chat_id': run['chat_id'], 'role': 'assistant', 'content': message,
                            'created_at': now(), 'metadata': {'run_id': run_id, 'pipeline_failure': True,
                                'turn_response': {'id': message_id, 'status': 'failed', 'message': message,
                                    'parts': parts, 'pending': [], 'actions': []}}})
                        self._record('node.error', run_id, node=run.get('stage'), level='ERROR')
                        self._record('run.failed', run_id, node=run.get('stage'), level='ERROR')

    async def snapshot(self, run_id):
        run = self.store.run(run_id)
        if run.get('runtime') != 'native' or self.graph is None:
            return public(run)
        def project(value):
            if value.get('knowledge_rebuild_required') and value['status'] == 'waiting':
                value = copy.deepcopy(value)
                previous = value.get('interrupt') or {}
                value['interrupt'] = {**previous, 'type': 'strategy_review',
                    'prompt_id': 'knowledge:' + value['id'] + ':' + str(value.get('knowledge_preference_version', 1)),
                    'title': '根据当前知识选择重新理解需求',
                    'message': '项目知识库使用范围已更改。回复同意后，将重新理解当前资料，再请你确认新结果；原成果保留为历史版本。'}
            return public(value)
        executing = self.tasks.get(run_id)
        if executing and not executing.done() and asyncio.current_task() is not executing:
            # A checkpoint observed mid-superstep may temporarily have no next
            # task. Only the executor may project completion after ainvoke.
            return project(run)
        state = await self.graph.aget_state(self._config(run_id))
        if run['status'] in ('cancelled', 'failed'):
            return project(run)
        pending = [i for task in state.tasks for i in task.interrupts]
        if pending and run_id not in self.tasks:
            value = self._interrupt_value(pending[0])
            changes = {'status': 'waiting', 'stage': value['type'], 'interrupt': value,
                       'interrupt_id': value['id'], 'current_artifact_id': value.get('artifact_id')}
        elif pending and asyncio.current_task() is self.tasks.get(run_id):
            value = self._interrupt_value(pending[0])
            changes = {'status': 'waiting', 'stage': value['type'], 'interrupt': value,
                       'interrupt_id': value['id'], 'current_artifact_id': value.get('artifact_id')}
        elif state.values and not state.next and not pending:
            changes = {'status': 'completed', 'stage': 'completed', 'interrupt': None,
                       'interrupt_id': None, 'current_artifact_id': state.values.get('current_artifact_id')}
        else:
            return project(run)
        if any(run.get(k) != v for k, v in changes.items()):
            run = self.store.update_run(run_id, **changes)
            if changes['status'] == 'waiting':
                self._record('node.interrupted', run_id, node=changes['stage'], stage=changes['stage'])
            elif changes['status'] == 'completed':
                self._record('run.completed', run_id, node='completed', stage='completed')
        return project(run)

    def _interrupt_value(self, native_interrupt):
        value = copy.deepcopy(native_interrupt.value)
        value['id'] = native_interrupt.id
        artifact_id = value.get('artifact_id')
        artifact = (self.store.revision(artifact_id, value['artifact_revision'])
                    if value.get('proposal_id') else self.store.get('artifact', artifact_id)) if artifact_id else None
        if artifact:
            value.update(artifact_revision=artifact['revision'], items=artifact['items'])
        value['prompt_id'] = 'pipeline:' + native_interrupt.id + ':' + str(value.get('artifact_revision', 0))
        if value.get('proposal_id'):
            value['prompt_id'] += ':' + value['proposal_id']
        return value

    async def resume(self, run_id, action='approved', expected_prompt_id=None, payload=None, *, prepare_resume=None):
        async with self._mutation_session(self.store.run(run_id)['chat_id']):
            return await self._resume(run_id, action, expected_prompt_id, payload, prepare_resume)

    async def _resume(self, run_id, action, expected_prompt_id, payload, prepare_resume=None):
        async with self._lock(run_id):
            if run_id in self.tasks and not self.tasks[run_id].done():
                raise DomainError('当前步骤仍在执行，请等待完成', 409)
            run = await self.snapshot(run_id)
            if run['status'] != 'waiting' or not run.get('interrupt'):
                raise DomainError('当前没有等待确认的步骤', 409)
            waiting = run['interrupt']
            if expected_prompt_id and expected_prompt_id not in (waiting['prompt_id'], waiting['id']):
                raise DomainError('这条回复对应的提示已更新，请查看当前待确认内容', 409)
            if action in ('cancel', 'cancelled'):
                self.store.update_run(run_id, status='cancelled', stage='cancelled', interrupt=None, interrupt_id=None)
                self._record('run.cancelled', run_id, node=waiting.get('type'))
                return public(self.store.run(run_id))
            if action not in ('approved', 'approve', 'continue', 'clarified', 'clarify', 'skip_to_cases', 'rejected'):
                raise DomainError('请说明修改意见，或明确同意当前内容')
            if action == 'skip_to_cases' and waiting['type'] != 'strategy_review':
                raise DomainError('只有确认需求理解时可以跳过场景；请先完成当前澄清或处理当前成果', 409)
            if action == 'rejected' and waiting['type'] != 'case_result_review':
                raise DomainError('当前结果尚未确认，请说明需要修改的内容', 409)
            if action == 'rejected' and not waiting.get('proposal_id'):
                raise DomainError('历史评审已写入用例，无法拒绝为建议；请修改当前用例或确认结束', 409)
            if (payload or {}).get('draft_only') and waiting['type'] not in ('strategy_review', 'scenario_review', 'case_draft_review'):
                raise DomainError('当前阶段不能切换为仅生成草稿', 409)
            if run.get('knowledge_rebuild_required'):
                if action in ('clarified', 'clarify', 'skip_to_cases', 'rejected'):
                    raise DomainError('知识库选择已更改，请先重新理解需求，再处理当前确认或跳过场景。', 409)
                return await self._restart_for_knowledge(self.store.run(run_id))
            clarification = action in ('clarified', 'clarify')
            if waiting['type'] == 'clarification':
                if not clarification:
                    raise DomainError('请先提交或采用澄清答案；确认需求理解是之后的独立步骤', 409)
                answers = (payload or {}).get('answers') or (payload or {}).get('answer')
                if not answers or (isinstance(answers, dict) and any(
                        not isinstance(value, str) or not value.strip() for value in answers.values())):
                    raise DomainError('请提供具体澄清答案，或明确采用当前建议')
            elif clarification:
                raise DomainError('当前不是澄清问题，请确认当前成果或直接说明修改意见', 409)
            if waiting['type'] == 'case_result_review' and waiting.get('proposal_id'):
                from .review_proposals import require_current_review
                require_current_review(self.store, run_id, waiting['proposal_id'])
            response = {**(payload or {}), 'action': action if action in ('skip_to_cases', 'rejected') else 'approved'}
            with self.store.transaction():
                if prepare_resume is not None:
                    prepare_resume()
                self.store.update_run(run_id, status='queued', stage='resuming', interrupt=None, interrupt_id=None)
            self._record('run.resumed', run_id, node=waiting.get('type'))
            self._schedule(run_id, Command(resume={waiting['id']: response}))
            return public(self.store.run(run_id))

    async def _restart_for_knowledge(self, run):
        # Start a fresh native graph so no cached analysis or inherited artifact
        # can smuggle an excluded fact back into the generation context.
        from .conversation_facts import sources_allowed
        request = copy.deepcopy(run.get('_request', {}))
        for key in ('artifact_id', 'artifact_revision', 'selected_ids', 'view_order',
                    '_pipeline_start', '_conversation_turn_id'):
            request.pop(key, None)
        request['source_ids'] = sources_allowed(self.store, run['chat_id'],
            list(dict.fromkeys(run.get('_source_ids', []) + run.get('_knowledge_enabled_source_ids', []))))
        request.update(_fresh_after_supplement=True, _knowledge_rebuild=True,
            content=request.get('content', '') + '\n根据当前项目知识库选择，重新理解需求，不继承旧成果中的业务假设。',
            intent='generate_case' if run['intent'] == 'review_case' else run['intent'],
            mode=run['mode'], stop_after=run.get('stop_after', 'review'),
            skip_scenarios=bool(run.get('skip_scenarios')))
        with self.store.transaction():
            self.store.update_run(run['id'], status='cancelled', stage='cancelled',
                interrupt=None, interrupt_id=None, superseded_reason='project_knowledge_changed')
            result = await self._start_run(run['chat_id'], request, schedule=False)
            self.store.update_run(run['id'], status='cancelled', replacement_run_id=result['id'])
        self._schedule(result['id'], self._initial(self.store.run(result['id'])))
        return result

    async def revise_review(self, run_id, feedback, *, selected_ids=None, expected_prompt_id=None,
                            artifact_id=None, artifact_revision=None, proposal_id=None):
        """Regenerate only a frozen review proposal; never approve or mutate cases."""
        if not isinstance(feedback, str) or not feedback.strip():
            raise DomainError('请说明需要调整的评审意见')
        run = self.store.run(run_id)
        async with self._mutation_session(run['chat_id']), self._lock(run_id):
            run = await self.snapshot(run_id)
            if run['status'] != 'waiting' or (run.get('interrupt') or {}).get('type') != 'case_result_review':
                raise DomainError('当前没有等待确认的评审建议', 409)
            gate = run['interrupt']
            current_prompt_id = gate.get('prompt_id') or gate.get('id')
            if expected_prompt_id is not None and current_prompt_id != expected_prompt_id:
                raise DomainError('评审提示已改变，请重新打开当前建议', 409)
            for supplied, current in ((artifact_id, gate.get('artifact_id')),
                                      (artifact_revision, gate.get('artifact_revision')),
                                      (proposal_id, gate.get('proposal_id'))):
                if supplied is not None and supplied != current:
                    raise DomainError('评审目标或版本已改变，请重新打开当前建议', 409)
            from .review_proposals import require_current_review
            proposal = require_current_review(self.store, run_id, gate.get('proposal_id'))
            if proposal['status'] != 'pending':
                raise DomainError('这份评审建议已处理，请查看当前结果', 409)
            cases = self.store.get('artifact', proposal['artifact_id'])
            available = {row['id'] for row in proposal['items']} | {row['id'] for row in cases['items']}
            if selected_ids is not None and (not isinstance(selected_ids, list) or not selected_ids
                    or any(not isinstance(item_id, str) for item_id in selected_ids)
                    or len(set(selected_ids)) != len(selected_ids) or not set(selected_ids) <= available):
                raise DomainError('所选条目不属于当前评审建议，请重新选择用例', 409)
            scope = {'proposal_id': proposal['id'], 'artifact_id': cases['id'],
                'artifact_revision': cases['revision'], 'prompt_id': current_prompt_id,
                'selected_ids': copy.deepcopy(selected_ids)}
            state = await self.graph.aget_state(self._config(run_id))
            values = {**state.values, 'review_ref': '', 'phase': 'review'}
            await self.graph.aupdate_state(self._config(run_id), values, as_node='case_draft_gate')
            self.store.update_run(run_id, status='queued', stage='review', interrupt=None, interrupt_id=None,
                                  review_feedback=feedback.strip(), _review_feedback_scope=scope, pause_after_step=True)
            self._schedule(run_id, None)
            return public(self.store.run(run_id))

    async def request_pause(self, run_id):
        run = self.store.run(run_id)
        if run['status'] == 'waiting':
            return await self.snapshot(run_id)
        if run['status'] not in ('queued', 'running'):
            raise DomainError('当前没有运行中的生成任务', 409)
        return public(self.store.update_run(run_id, pause_after_step=True))

    async def cancel(self, run_id):
        run = self.store.run(run_id)
        if run['status'] in ('completed', 'cancelled'):
            return public(run)
        self.store.update_run(run_id, status='cancelled', stage='cancelled', interrupt=None, interrupt_id=None)
        self._record('node.cancelled', run_id, node=run.get('stage'))
        self._record('run.cancelled', run_id, node=run.get('stage'))
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return public(self.store.run(run_id))

    async def retry(self, run_id):
        async with self._mutation_session(self.store.run(run_id)['chat_id']), self._lock(run_id):
            run = self.store.run(run_id)
            if run.get('runtime') != 'native' or run.get('migration', {}).get('status') == 'restart_required':
                raise DomainError('此历史任务需要重新开始；已有资料和成果均已保留', 409)
            if run['status'] != 'failed':
                raise DomainError('只有失败步骤可以重试', 409)
            if any(r['id'] != run_id for r in self.store.runs(chat_id=run['chat_id'],
                       statuses=('queued', 'running', 'waiting'))):
                raise DomainError('当前对话已有另一个活动任务，请先处理当前任务', 409)
            if run.get('knowledge_rebuild_required'):
                return await self._restart_for_knowledge(run)
            self.store.update_run(run_id, status='queued', error=None,
                _content_retry_round=run.get('_content_retry_round', 0) + 1, repair_progress=None,
                failed_node=None, candidate_id=None, failure_category=None, failure_call_id=None)
            self._record('run.retried', run_id, node=run.get('failed_node') or run.get('stage'))
            state = await self.graph.aget_state(self._config(run_id))
            self._schedule(run_id, None if state.values else self._initial(run))
            return public(self.store.run(run_id))

    async def on_artifact_changed(self, artifact):
        """Rewind the actual graph to the changed artifact's confirmation gate."""
        mapping = {'analysis': ('analysis_ref', 'clarification_gate', 'strategy_review'),
                   'scenarios': ('scenario_ref', 'scenarios', 'scenario_review'),
                   'cases': ('cases_ref', 'cases', 'case_draft_review')}
        if artifact['type'] not in mapping:
            return []
        results = []
        active = self.store.runs(chat_id=artifact['chat_id'], statuses=('queued', 'running', 'waiting'))
        candidates = active or self.store.runs(chat_id=artifact['chat_id'], statuses=('completed', 'failed'))
        candidates = sorted(candidates, key=lambda value: value['created_at'], reverse=True)
        if not active:
            candidates = candidates[:1]
        for run in candidates:
            if run.get('runtime') != 'native':
                continue
            async with self._lock(run['id']):
                state = await self.graph.aget_state(self._config(run['id']))
                key, previous_node, phase = mapping[artifact['type']]
                if state.values.get(key) != artifact['id']:
                    continue
                if self.store.run(run['id'])['status'] not in ('waiting', 'completed', 'failed'):
                    raise DomainError('任务正在生成，请等待当前步骤完成后修改', 409)
                current_run = self.store.run(run['id'])
                self.store.update_run(run['id'],
                    _source_ids=list(dict.fromkeys(current_run.get('_source_ids', []) + artifact.get('_source_ids', []))),
                    _source_roles={**current_run.get('_source_roles', {}), **artifact.get('_source_roles', {})})
                native_pending = [i for task in state.tasks for i in task.interrupts]
                current_kind = native_pending[0].value.get('type') if native_pending else None
                order = {'clarification': 0, 'strategy_review': 1, 'scenario_review': 2,
                         'case_draft_review': 3, 'case_result_review': 4}
                if order.get(current_kind, 5) < order[phase]:
                    # Editing a downstream row cannot approve or skip an
                    # already pending upstream confirmation.
                    results.append(await self.snapshot(run['id']))
                    break
                values = {key: artifact['id'], 'current_artifact_id': artifact['id'], 'phase': phase,
                          'review_ref': ''}
                if artifact['type'] == 'cases':
                    if not artifact['items']:
                        await self.graph.aupdate_state(self._config(run['id']), values, as_node='finish')
                        self.store.update_run(run['id'], status='running', stage='completed',
                            interrupt=None, interrupt_id=None, current_artifact_id=artifact['id'],
                            error=None, failed_node=None, review_proposal_id=None)
                        results.append(await self.snapshot(run['id']))
                        break
                    await self.graph.aupdate_state(self._config(run['id']), values, as_node='case_draft_gate')
                    self.store.update_run(run['id'], status='queued', stage='review', interrupt=None,
                        interrupt_id=None, pause_after_step=True, error=None, failed_node=None)
                    self._schedule(run['id'], None)
                    results.append(public(self.store.run(run['id'])))
                    break
                await self.graph.aupdate_state(self._config(run['id']), values, as_node=previous_node)
                self.store.update_run(run['id'], status='running', stage=phase,
                                      interrupt=None, interrupt_id=None, pause_after_step=True,
                                      error=None, failed_node=None)
                # Gate-only invocation is short and must establish a new native
                # interrupt before this edit tool returns to the chat agent.
                await self.graph.ainvoke(None, self._config(run['id']))
                results.append(await self.snapshot(run['id']))
                break
        return results

    async def restore_waiting(self, run_id, values, gate_type):
        """Upgrade a known persisted review position into a real native gate."""
        predecessor = {'clarification': 'understand', 'strategy_review': 'clarification_gate', 'scenario_review': 'scenarios',
                       'case_draft_review': 'cases', 'case_result_review': 'review'}
        if gate_type not in predecessor:
            raise DomainError('此旧确认位置需要先检查资料后重新开始', 409)
        run = self.store.run(run_id)
        async with self._mutation_session(run['chat_id']), self._lock(run_id):
            pointer_values = {key: value for key, value in values.items() if key in PipelineState.__annotations__}
            pointer_values.update(run_id=run_id, project_id=run['project_id'], chat_id=run['chat_id'], phase=gate_type)
            await self.graph.aupdate_state(self._config(run_id), pointer_values, as_node=predecessor[gate_type])
            self.store.update_run(run_id, runtime='native', graph_version=9, status='running',
                                  pause_after_step=True, interrupt=None, interrupt_id=None)
            await self.graph.ainvoke(None, self._config(run_id))
            return await self.snapshot(run_id)

    def _after_understanding(self, state):
        run = self.store.run(state['run_id'])
        if run.get('stop_after') == 'analysis':
            return 'finish'
        return 'skip_to_cases' if run.get('skip_scenarios') else 'scenarios'

    def _after(self, state, phase, next_node):
        run = self.store.run(state['run_id'])
        return 'finish' if run.get('stop_after') == phase else next_node

    def _run(self, state, stage):
        run = self.store.run(state['run_id'])
        if run['status'] == 'cancelled':
            raise DomainError('任务已取消', 409)
        if stage in {'understand', 'apply_clarification', 'scenarios', 'cases', 'review', 'apply_review'}:
            self._record('node.start', run['id'], node=stage, stage=stage)
        return self.store.update_run(run['id'], stage=stage,
                                     progress={'phase': stage, 'label': stage})

    def _record(self, event, run_id, **fields):
        diagnostics = getattr(self, 'diagnostics', None)
        if diagnostics:
            with suppress(Exception):
                diagnostics.record(event, run_id=run_id, **fields)

    def _artifact(self, state, key):
        return self.store.get('artifact', state[key])

    def _save_reference(self, state, artifact, key, phase):
        with self.store.transaction():
            run = self.store.run(state['run_id'])
            if run['status'] == 'cancelled':
                raise DomainError('任务已取消', 409)
            artifact = self.store.get('artifact', artifact['id'])
            if not artifact.get('_visible'):
                self.store.put('artifact', {**artifact, '_visible': True})
            self.store.update_run(run['id'], artifact_ids=list(dict.fromkeys(run.get('artifact_ids', []) + [artifact['id']])),
                                  current_artifact_id=artifact['id'])
            event_id = ':'.join(('stage', run['id'], phase, artifact['id'], str(artifact['revision'])))
            try:
                self.store.get('pipeline_result', event_id)
            except DomainError as exc:
                if exc.status != 404:
                    raise
                self.store.put('pipeline_result', {'id': event_id, 'run_id': run['id'],
                    'chat_id': run['chat_id'], 'project_id': run['project_id'], 'phase': phase,
                    'artifact_id': artifact['id'], 'revision': artifact['revision'], 'created_at': now()})
        self._record('node.complete', run['id'], node=run.get('stage'), stage=run.get('stage'))
        return {key: artifact['id'], 'current_artifact_id': artifact['id'], 'phase': phase}

    async def _node_understand(self, state):
        run = self._run(state, 'understand')
        return self._save_reference(state, await self.business.understand(run), 'analysis_ref', 'understood')

    def _questions(self, artifact):
        report = artifact.get('report') or {}
        from .clarification import pending_questions
        recommendations = {value.get('question'): value for value in report.get('question_suggestions', [])
                           if isinstance(value, dict)}
        questions = []
        for value in pending_questions(report):
            question = value['question']
            recommendation = recommendations.get(question, {})
            questions.append({**recommendation, **value, 'question': question,
                              'suggestion': value.get('suggestion') or value.get('suggested_answer') or recommendation.get('answer') or value.get('assumption') or
                              '暂按当前需求已描述的范围执行，未说明的条件标记为待确认。'})
        return questions

    async def _node_clarification_gate(self, state):
        run = self._run(state, 'clarification')
        artifact = self._artifact(state, 'analysis_ref')
        questions = self._questions(artifact)
        if not questions:
            return {'phase': 'understood'}
        response = interrupt({'type': 'clarification', 'title': '补充需求说明',
                              'artifact_id': artifact['id'], 'questions': questions,
                              'message': '请回答澄清问题，或采用建议答案。提交答案后更新理解；人工模式下还需单独确认更新后的需求理解。'})
        answers = response.get('answers') or response.get('answer')
        if not answers:
            raise DomainError('请提供具体澄清答案，不能把阶段确认当作答案提交')
        if isinstance(answers, dict):
            questions_by_id = {q['id']: q['question'] for q in questions}
            answers = {questions_by_id.get(key, key): value for key, value in answers.items()}
        self.store.update_run(run['id'], clarification_answers=answers,
                              clarification_save_to_project=response.get('save_to_project', True),
                              pause_after_step=False)
        return {'phase': 'clarification_answered'}

    async def _node_apply_clarification(self, state):
        run = self._run(state, 'apply_clarification')
        artifact = self._artifact(state, 'analysis_ref')
        answers = run.get('clarification_answers')
        apply = getattr(self.business, 'clarify', None) or getattr(self.business, 'apply_clarification', None)
        if apply is None:
            raise DomainError('需求澄清处理服务尚未配置')
        result = await apply(run, artifact, answers)
        return self._save_reference(state, result, 'analysis_ref', 'understood')

    async def _gate(self, state, name):
        kind, key, title, next_stage = GATES[name]
        run = self._run(state, kind)
        artifact = self._artifact(state, key)
        message = MESSAGES[kind]
        if name == 'understanding_gate' and run.get('skip_scenarios'):
            next_stage = 'cases'
            message = '请检查需求理解。确认后会跳过场景，直接生成用例' + ('草稿，不进行 AI 评审。' if run.get('stop_after') == 'cases' else '并提出 AI 评审建议。')
        if run['mode'] in ('hitp', 'human') or run.get('pause_after_step') or (
                name == 'understanding_gate' and run.get('_request', {}).get('_knowledge_rebuild')):
            response = interrupt({'type': kind, 'artifact_id': artifact['id'], 'title': title,
                                  'message': message, 'next_stage': next_stage,
                                  'confirm_label': '同意，继续', 'stop_after': run.get('stop_after')})
            action = response.get('action')
            if action != 'approved' and not (name == 'understanding_gate' and action == 'skip_to_cases'):
                raise DomainError('请明确确认当前内容')
            changes = {'pause_after_step': False}
            if action == 'skip_to_cases':
                if self._questions(artifact):
                    raise DomainError('请先回答澄清问题，再跳过场景生成用例', 409)
                changes.update(skip_scenarios=True, stop_after='cases' if (response.get('draft_only') or run.get('stop_after') == 'cases') else 'review')
            elif response.get('draft_only'):
                changes['stop_after'] = 'cases'
            if response.get('draft_only'):
                changes['explicit_draft_only'] = True
            self.store.update_run(run['id'], **changes)
        return {'phase': kind, 'current_artifact_id': artifact['id']}

    async def _node_understanding_gate(self, state):
        return await self._gate(state, 'understanding_gate')

    async def _node_scenario_gate(self, state):
        return await self._gate(state, 'scenario_gate')

    async def _node_case_draft_gate(self, state):
        run = self.store.run(state['run_id'])
        # A newly requested draft is a finished deliverable, not another approval.
        if (run.get('graph_version', 0) >= 10 or run.get('explicit_draft_only')) and run.get('stop_after') == 'cases':
            return {'phase': 'cases_generated'}
        # Preserve already saved legacy checkpoints and an explicit runtime pause.
        if run.get('graph_version', 0) < 9 or run.get('stop_after') == 'cases' or run.get('pause_after_step'):
            return await self._gate(state, 'case_draft_gate')
        return {'phase': 'cases_generated'}

    async def _node_review_result_gate(self, state):
        if not state.get('review_ref'):
            # Old checkpoints already saved reviewed cases. Keep that approval,
            # then finish without applying the old modifications a second time.
            return await self._gate(state, 'review_result_gate')
        from .review_proposals import require_current_review
        run = self._run(state, 'case_result_review')
        saved = self.store.get('review_proposal', state['review_ref'])
        if saved.get('status') == 'rejected' and saved.get('run_id') == run['id']:
            return {'phase': 'review_rejected', 'current_artifact_id': saved['artifact_id']}
        proposal = require_current_review(self.store, run['id'], state['review_ref'])
        if run['mode'] in ('hitp', 'human') or run.get('pause_after_step'):
            response = interrupt({'type': 'case_result_review', 'artifact_id': proposal['artifact_id'],
                'artifact_revision': proposal['artifact_revision'], 'proposal_id': proposal['id'],
                'title': '确认评审建议', 'message': MESSAGES['case_result_review'],
                'next_stage': 'apply_review', 'confirm_label': '确认评审建议并修改用例'})
            if response.get('action') == 'rejected':
                self.store.put('review_proposal', {**proposal, 'status': 'rejected', 'rejected_at': now()})
                self.store.update_run(run['id'], pause_after_step=False)
                self._record('review.rejected', run['id'], proposal_id=proposal['id'])
                return {'phase': 'review_rejected', 'current_artifact_id': proposal['artifact_id']}
            if response.get('action') != 'approved':
                raise DomainError('请明确确认当前评审建议')
            self.store.update_run(run['id'], pause_after_step=False)
        return {'phase': 'review_approved', 'current_artifact_id': proposal['artifact_id']}

    async def _node_scenarios(self, state):
        run = self._run(state, 'scenarios')
        artifact = await self.business.scenarios(run, self._artifact(state, 'analysis_ref'))
        return self._save_reference(state, artifact, 'scenario_ref', 'scenarios_generated')

    async def _node_cases(self, state):
        run = self._run(state, 'cases')
        artifact = await self.business.cases(run, self._artifact(state, 'analysis_ref'), self._artifact(state, 'scenario_ref'))
        return self._save_reference(state, artifact, 'cases_ref', 'cases_generated')

    async def _node_direct_cases(self, state):
        run = self._run(state, 'cases')
        artifact = await self.business.direct_cases(run, self._artifact(state, 'analysis_ref'))
        return {**self._save_reference(state, artifact, 'cases_ref', 'cases_generated'), 'scenario_ref': ''}

    async def _node_review(self, state):
        run = self._run(state, 'review')
        proposal = await self.business.propose_review(run, self._artifact(state, 'cases_ref'),
                                                     feedback=run.get('review_feedback', ''))
        with self.store.transaction():
            if self.store.run(run['id'])['status'] == 'cancelled':
                raise DomainError('任务已取消', 409)
            previous_id = self.store.run(run['id']).get('review_proposal_id')
            if previous_id and previous_id != proposal['id']:
                previous = self.store.get('review_proposal', previous_id)
                if previous['status'] == 'pending':
                    self.store.put('review_proposal', {**previous, 'status': 'superseded', 'superseded_by': proposal['id']})
            self.store.update_run(run['id'], review_proposal_id=proposal['id'])
            event_id = 'review-event:' + proposal['id']
            self.store.put('pipeline_result', {'id': event_id, 'run_id': run['id'], 'chat_id': run['chat_id'],
                'project_id': run['project_id'], 'phase': 'review_proposed', 'proposal_id': proposal['id'],
                'requires_confirmation': run['mode'] in ('human', 'hitp') or bool(run.get('pause_after_step')),
                'artifact_id': proposal['artifact_id'], 'revision': proposal['artifact_revision'],
                'created_at': proposal['created_at']})
        self._record('node.complete', run['id'], node='review', stage='review')
        return {'review_ref': proposal['id'], 'current_artifact_id': proposal['artifact_id'], 'phase': 'review_proposed'}

    async def _node_apply_review(self, state):
        if not state.get('review_ref'):
            return {'phase': 'reviewed'}
        run = self._run(state, 'apply_review')
        artifact = self.business.apply_review_proposal(run, state['review_ref'])
        return self._save_reference(state, artifact, 'cases_ref', 'reviewed')

    async def _node_finish(self, state):
        self._run(state, 'completed')
        return {'phase': 'completed'}
