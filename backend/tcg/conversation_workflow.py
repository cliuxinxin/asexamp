"""Registered conversation controls around the existing fixed authoring graph."""
import copy
import json

from .clarification import get_draft, update_draft, save_draft, share_draft, register_routes
from .schemas import DomainError
from .storage import public


def _schema(properties):
    return {'type': 'object', 'properties': properties, 'additionalProperties': False}


_RUN = {'run_id': {'type': 'string', 'description': '当前对话的真实工作流 ID；省略时使用唯一活动任务。'},
        'expected_control_version': {'type': 'integer', 'description': '控制指令版本；后来的暂停使旧继续失效。'}}
_GOAL = {'stop_after': {'type': 'string', 'enum': ['analysis', 'scenarios', 'cases', 'review', 'complete'],
                      'description': '整个任务的持久停止位置，补资料与重试后仍保留。'},
         'scope': {'description': '后续生成的业务范围；不能将暂停口令写成业务事实。'}}
_DRAFT = {**_RUN, 'expected_revision': {'type': 'integer', 'description': '当前澄清草稿修订号。'},
          'question_set_version': {'type': 'string'}, 'answer': {'type': 'string'},
          'answers': {'type': 'object', 'additionalProperties': {'type': 'string'}},
          'adopt_ids': {'type': 'array', 'items': {'type': 'string'}}, 'adopt_all': {'type': 'boolean', 'default': False}}
CAPABILITIES = {
    'workflow.start': {'effect': 'control', 'description': '开始需求测试设计，保存长期目标；已有确认任务时复用该任务。',
        'parameters': _schema({**_RUN, **_GOAL, 'content': {'type': 'string'}, 'intent': {'type': 'string', 'default': 'generate_case'},
            'goal': {'type': 'string'},
            'mode': {'type': 'string', 'enum': ['auto', 'hitp'], 'default': 'hitp'}, 'profile_id': {'type': 'string'},
            'source_ids': {'type': 'array', 'items': {'type': 'string'}}, 'artifact_id': {'type': 'string'},
            'selected_ids': {'type': 'array', 'items': {'type': 'string'}}, 'depth': {'type': 'string'},
            'profile_override': {'type': 'object'}, 'case_types': {'type': 'array', 'items': {'type': 'string'}},
            'as_requirement': {'type': 'boolean'}, 'approved': {'type': 'boolean'}})},
    'workflow.continue': {'effect': 'control', 'description': '明确确认当前真实 interrupt；复用已保存澄清，只推进当前节点。',
        'parameters': _schema({**_RUN, **_GOAL, 'interrupt_id': {'type': 'string'}, 'expected_revision': {'type': 'integer'}, 'approved': {'type': 'boolean'},
            'answer': {'type': 'string'}, 'save_to_project': {'type': 'boolean', 'default': False},
            'source_ids': {'type': 'array', 'items': {'type': 'string'}}, 'depth': {'type': 'string'}})},
    'workflow.pause': {'effect': 'control', 'description': '暂停整个生成任务，在当前步骤安全保存后停止；不把控制口令保存为需求。', 'parameters': _schema(_RUN)},
    'workflow.cancel': {'effect': 'control', 'description': '停止整个任务并拒绝晚到结果；撤销预览应使用 artifact.discard。', 'parameters': _schema(_RUN)},
    'workflow.retry': {'effect': 'control', 'description': '从失败阶段恢复，保留已保存成果、澄清和持久停止位置。', 'parameters': _schema(_RUN)},
    'workflow.update_scope': {'effect': 'control', 'description': '修改整个任务的后续范围或停止位置；运行中先在安全边界协调。',
        'parameters': _schema({**_RUN, **_GOAL, 'source_ids': {'type': 'array', 'items': {'type': 'string'}},
            'goal': {'type': 'string', 'enum': ['review_requirement', 'generate_scenario', 'generate_case']}})},
    'clarification.adopt': {'effect': 'write', 'description': '采用建议或编辑同一持久草稿；不会提交、共享或继续。', 'parameters': _schema(_DRAFT)},
    'clarification.save': {'effect': 'write', 'description': '提交当前澄清草稿为用户确认的依据；保留当前工作流等待。',
        'parameters': _schema({**_DRAFT, 'share': {'type': 'boolean', 'default': False}})},
    'clarification.share': {'effect': 'write', 'description': '将已提交的澄清共享到本项目；不推进生成。', 'parameters': _schema(_DRAFT)},
}


from .capability_contracts import contract
CAPABILITIES = {name: contract(definition, context_policy='run_control') for name, definition in CAPABILITIES.items()}


def _result(status, message, run=None, parts=None, **extra):
    return {'status': status, 'message': message, 'parts': parts or [],
            **({'run': public(run)} if run else {}), **extra}


def _receipt(store, turn_id, result):
    if turn_id:
        from .conversation_receipts import commit_result
        commit_result(store, turn_id, result)
    return result


def _resolve_run(store, chat, args):
    if args.get('run_id'):
        run = store.run(args['run_id'])
        if run['chat_id'] != chat['id'] or run['project_id'] != chat['project_id']:
            raise DomainError('任务不属于当前对话')
        return run
    runs = store.runs(chat_id=chat['id'])
    active = [run for run in runs if run['status'] in ('queued', 'running', 'waiting')]
    if len(active) == 1:
        return active[0]
    if not active and runs:
        return max(runs, key=lambda run: (run['created_at'], run['id']))
    raise DomainError('当前没有可操作的工作流，请先开始任务', 409)


def _goals(args):
    changes = {}
    if 'stop_after' in args:
        if args['stop_after'] not in ('analysis', 'scenarios', 'cases', 'review', 'complete'):
            raise DomainError('stop_after 必须是 analysis、scenarios、cases、review 或 complete')
        changes['stop_after'] = args['stop_after']
    if 'goal' in args:
        goals = {'review_requirement': 'analysis', 'generate_scenario': 'scenarios', 'generate_case': 'review'}
        if args['goal'] not in goals:
            raise DomainError('不支持的工作流目标')
        changes.setdefault('stop_after', goals[args['goal']])
        changes['goal'] = args['goal']
    if 'scope' in args:
        if not isinstance(args['scope'], (str, dict)):
            raise DomainError('scope 需要业务范围文本或对象')
        changes['scope'] = copy.deepcopy(args['scope'])
    return changes


def _apply_scope(store, run, args):
    changes = _goals(args)
    if 'source_ids' in args:
        values = args['source_ids']
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(value, str) for value in values):
            raise DomainError('source_ids 必须为最多 100 个真实来源 ID')
        for sid in values:
            source = store.get('source', sid)
            if source['project_id'] != run['project_id'] or not source.get('_active'):
                raise DomainError('资料不属于当前项目或已经停用')
        changes['_source_ids'] = list(dict.fromkeys(run['_source_ids'] + values))
        changes['_source_roles'] = {**run.get('_source_roles', {}),
            **{sid: store.get('source', sid)['role'] for sid in values}}
    if changes:
        if 'scope' in changes:
            changes['_profile'] = {**run['_profile'], 'scope': changes['scope'] if isinstance(changes['scope'], str)
                else json.dumps(changes['scope'], ensure_ascii=False)}
        input_version = run.get('input_version', 0) + 1
        changes.update(input_version=input_version, _input_version=input_version)
        changes['_request'] = {**run['_request'], **{key: changes[key] for key in ('stop_after', 'goal', 'scope') if key in changes}}
        run = store.update_run(run['id'], **changes)
    return run


async def execute(store, engine, chat, name, args, turn_id=None):
    if name not in CAPABILITIES or not isinstance(args, dict):
        raise DomainError('未知工作流操作或参数无效')
    with store.transaction():
        if name == 'workflow.start':
            active = store.runs(chat_id=chat['id'], statuses=('queued', 'running', 'waiting'))
            if active:
                run = active[0]
                if run['status'] != 'waiting':
                    return _receipt(store, turn_id, _result('needs_input', '当前任务仍在运行；请明确修改范围或暂停。', run))
                target = args.get('goal') or args.get('intent', 'generate_case')
                gate = run.get('interrupt', {}).get('type')
                if (target == 'generate_case' and gate == 'scenario_review') or (
                        target in ('generate_scenario', 'generate_case') and gate == 'strategy_review'):
                    args = {**args, 'run_id': run['id'], 'stop_after': args.get('stop_after',
                        'scenarios' if target == 'generate_scenario' else 'review'), 'approved': True}
                    name = 'workflow.continue'
                else:
                    return _receipt(store, turn_id, _result('needs_confirmation', '请先确认当前工作流节点，再继续后续阶段。', run,
                        pending=[{'type': 'workflow', 'run_id': run['id'], 'interrupt_id': run.get('_interrupt_id')}]))
            else:
                content = args.get('content', '').strip()
                if not content:
                    raise DomainError('请说明本次测试设计目标')
                requested_intent = args.get('goal') or args.get('intent', 'generate_case')
                if requested_intent not in ('auto', 'review_requirement', 'generate_scenario', 'generate_case', 'review_case'):
                    raise DomainError('当前能力只启动测试设计或用例评审工作流')
                goal = args.get('stop_after') or {'generate_scenario': 'scenarios', 'review_requirement': 'analysis'}.get(requested_intent, 'review')
                _goals({'stop_after': goal})
                request = {key: copy.deepcopy(value) for key, value in args.items() if key in
                    ('profile_id', 'source_ids', 'artifact_id', 'selected_ids', 'depth', 'profile_override', 'case_types', 'as_requirement')}
                current_chat = store.get('chat', chat['id'])
                if not request.get('profile_id') and current_chat.get('profile_id'):
                    request['profile_id'] = current_chat['profile_id']
                if 'source_ids' not in request and current_chat.get('source_ids'):
                    from .project_context import shared_sources
                    request['source_ids'] = list(dict.fromkeys(current_chat['source_ids'] +
                        [s['id'] for s in shared_sources(store, chat['project_id'])]))
                # Keep a single graph resumable after its persistent scenario stop.
                graph_intent = 'generate_case' if requested_intent in ('generate_scenario', 'review_requirement') else requested_intent
                # A fresh request with new requirements must not silently pick
                # a historical scenario. The controller resolves explicit focus.
                request.update(content=content, intent=graph_intent, mode=args.get('mode', 'hitp'), experience='reliable', stop_after=goal)
                if request['mode'] not in ('auto', 'hitp'):
                    raise DomainError('mode 必须为 auto 或 hitp')
                if args.get('conversation_turn_id'):
                    request['_conversation_turn_id'] = args['conversation_turn_id']
                _, run = store.create_run(chat['id'], request)
                scope=args.get('scope',run['_profile'].get('scope',''))
                _goals({'scope':scope})
                run = store.update_run(run['id'], stop_after=goal, goal=requested_intent, scope=scope,
                    control_version=0, _control_version=0, input_version=0, _input_version=0,
                    _profile={**run['_profile'],'scope':scope if isinstance(scope,str) else json.dumps(scope,ensure_ascii=False)},
                    _source_roles={**run.get('_source_roles', {}), **{sid: role for sid, role in current_chat.get('_source_roles', {}).items()
                        if sid in run['_source_ids']}})
                result = _receipt(store, turn_id, _result('succeeded', '已开始测试设计，目标和停止位置已保存。', run))
                engine.schedule(run['id'])
                return result
        run = _resolve_run(store, chat, args)
        rid = run['id']
        expected = args.get('expected_control_version')
        if expected is not None and expected != run.get('control_version', 0):
            return _receipt(store, turn_id, _result('cancelled', '后来的工作流控制指令已替代这次操作。', run))
        if name.startswith('clarification.'):
            fn = {'clarification.adopt': update_draft, 'clarification.save': save_draft, 'clarification.share': share_draft}[name]
            draft_args = dict(args)
            if name == 'clarification.adopt' and not any(key in draft_args for key in ('adopt_ids', 'adopt_all', 'answer', 'answers')):
                draft_args['adopt_all'] = True
            draft = fn(store, rid, draft_args)
            if name == 'clarification.save' and args.get('share'):
                draft = share_draft(store, rid)
            message = {'clarification.adopt': '已更新澄清草稿，仍可编辑；尚未提交或继续。',
                       'clarification.save': '澄清答案已提交并共享到项目，工作流仍等待确认。' if args.get('share') else '澄清答案已提交保存，工作流仍等待确认。',
                       'clarification.share': '已将提交的澄清保存到项目，工作流保持原状态。'}[name]
            return _receipt(store, turn_id, _result('succeeded', message, store.run(rid), [{'type': 'clarification_draft', 'draft': draft}]))
        if name == 'workflow.pause':
            if run['status'] in ('completed', 'cancelled', 'failed'):
                return _receipt(store, turn_id, _result('succeeded', '任务当前没有继续生成，已保留已有成果。', run))
            run = engine.request_boundary(rid, reason='pause', command_id=turn_id)
            return _receipt(store, turn_id, _result('deferred' if run['status'] in ('queued', 'running') else 'succeeded',
                '已记录暂停；当前步骤保存后停在安全边界。' if run['status'] in ('queued', 'running') else '已保持当前等待，后续继续请求需重新确认。', run))
        if name == 'workflow.cancel':
            run = engine.cancel(rid)
            return _receipt(store, turn_id, _result('succeeded', '整个任务已停止，已保存成果保留。', run))
        if name == 'workflow.update_scope':
            _goals(args)
            if 'source_ids' in args:
                ids = args['source_ids']
                if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(sid, str) for sid in ids):
                    raise DomainError('source_ids 必须为最多 100 个真实来源 ID')
                for sid in ids:
                    source = store.get('source', sid)
                    if source['project_id'] != run['project_id'] or not source.get('_active'):
                        raise DomainError('资料不属于当前项目或已经停用')
            if run['status'] in ('queued', 'running'):
                pending = list(run.get('_pending_scope_updates', []))
                pending.append({'arguments': copy.deepcopy(args), 'command_id': turn_id})
                store.update_run(rid, _pending_scope_updates=pending)
                run = engine.request_boundary(rid, reason='scope', command_id=turn_id)
                return _receipt(store, turn_id, _result('deferred', '范围变更已记录，将在当前步骤保存后的安全边界生效。', run))
            run = _apply_scope(store, run, args)
            return _receipt(store, turn_id, _result('succeeded', '后续业务范围和停止条件已保存；当前确认节点保留。', run))
        if name == 'workflow.retry':
            run = engine.retry(rid)
            return _receipt(store, turn_id, _result('succeeded', '已从失败阶段重试，已有成果和停止位置保留。', run))
        if name == 'workflow.continue':
            if run['status'] != 'waiting':
                raise DomainError('当前工作流未等待继续', 409)
            if args.get('interrupt_id') and args['interrupt_id'] != run.get('_interrupt_id'):
                raise DomainError('确认节点已改变，请确认当前节点', 409)
            from .workflow_bindings import validate_confirmation
            validate_confirmation(store,run,args)
            if any(key in args for key in ('stop_after', 'scope')):
                run = _apply_scope(store, run, args)
                args={**args,'expected_control_version':run.get('control_version',0)}
            kind = run.get('interrupt', {}).get('type')
            if (kind == 'scenario_review' and run.get('stop_after') == 'scenarios') or (
                    kind == 'strategy_review' and run.get('stop_after') == 'analysis') or (
                    kind == 'workflow_paused' and run.get('interrupt', {}).get('reason') == 'stop_after'
                    and run.get('stop_after') == 'cases'):
                return _receipt(store, turn_id, _result('needs_input',
                    '当前任务已达到保存的停止位置；请明确允许后续生成并修改任务目标后继续。', run))
            response = {key: args[key] for key in ('approved', 'answer', 'source_ids', 'depth', 'interrupt_id', 'expected_revision', 'expected_control_version') if key in args}
            if kind == 'clarification':
                if args.get('answer', '').strip():
                    draft = save_draft(store, rid, {'answer': args['answer']})
                else:
                    draft = get_draft(store, rid)
                    if not draft['submitted']:
                        if not draft['answer'].strip():
                            return _receipt(store, turn_id, _result('needs_input', '请填写或采用澄清答案后继续。', run,
                                [{'type': 'clarification_draft', 'draft': draft}]))
                        # An explicit instruction to continue with these answers
                        # authorizes submission, while adoption alone never does.
                        draft = save_draft(store, rid)
                if args.get('save_to_project'):
                    draft = share_draft(store, rid)
                response.update(answer=draft['answer'], source_id=draft['source_id'], save_to_project=False)
            elif kind in ('scenario_review', 'strategy_review'):
                response.setdefault('approved', True)
            run = engine.resume(rid, response)
            return _receipt(store, turn_id, _result('succeeded', '已确认当前节点并继续，持久停止条件仍生效。', run))
    raise DomainError('无法执行工作流操作')


def apply_pending_scope(store, run_id):
    """Called only after the runner persisted a genuine safe boundary."""
    with store.transaction():
        run = store.run(run_id)
        if run['status'] != 'waiting':
            return
        for pending in run.get('_pending_scope_updates', []):
            if pending.get('command_id'):
                command = store.get('conversation_command', pending['command_id'])
                if command['status'] in ('cancelled', 'succeeded'):
                    continue
            run = _apply_scope(store, store.run(run_id), pending['arguments'])
            _receipt(store, pending.get('command_id'), _result('succeeded', '排队的范围变更已在安全边界保存。', run))
        if run.get('_pending_scope_updates'):
            store.update_run(run_id, _pending_scope_updates=[])
