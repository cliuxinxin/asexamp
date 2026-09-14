"""Persistent capability queue around the existing chat agent and pipeline.

Plans contain only instructions, scoped IDs and compact receipts. Native pipeline
checkpoints and business approval prompts remain the sole workflow authority.
"""
import asyncio
import copy
import json
import re
from contextlib import suppress, nullcontext
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from .native_views import chat_context, current_prompt
from .prompt_loader import load_prompt, prompt_templates
from .schemas import DomainError
from .storage import now, uid

READS = frozenset({'list_context_tool', 'list_artifacts_tool', 'list_sources_tool',
    'read_artifact_tool', 'read_review_proposal_tool', 'read_profile_tool', 'read_knowledge_tool'})
CAPABILITIES = {
    'answer': READS | {'analyze_artifact_tool'},
    'estimate': READS | {'estimate_workload_tool'},
    'artifact_edit': READS | {'modify_artifact_tool', 'update_from_sources_tool'},
    'case_columns': READS | {'modify_case_columns_tool'},
    'profile_edit': READS | {'modify_profile_tool'},
    'template_learn': READS | {'learn_template_tool'},
    'knowledge': READS | {'add_knowledge_tool'},
    'clarification': READS | {'answer_clarification_tool'},
    'pipeline_start': READS | {'start_pipeline_tool'},
    'pipeline_control': READS | {'control_pipeline_tool', 'resume_pipeline_tool', 'answer_clarification_tool'},
    'review_edit': READS | {'revise_review_tool'},
    'samples': READS | {'save_samples_tool'},
    'template_fill': READS | {'complete_template_fields_tool'},
    'export': READS | {'export_artifact_tool'},
    'current_control': frozenset(),
}
Capability = Literal['answer', 'estimate', 'artifact_edit', 'case_columns', 'profile_edit',
    'template_learn', 'knowledge', 'clarification', 'pipeline_start', 'pipeline_control', 'review_edit',
    'samples', 'template_fill', 'export', 'current_control']
ACTIVE = {'running', 'waiting_confirmation', 'waiting_pipeline', 'failed', 'blocked'}
READ_CAPABILITIES = {'answer', 'estimate'}


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    capability: Capability
    instruction: str = Field(min_length=1, max_length=3000)


class PlannedRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=160)
    steps: list[PlannedStep] = Field(min_length=1, max_length=12)


@tool(args_schema=PlannedRequest)
def submit_execution_plan(title: str, steps: list[PlannedStep]) -> dict:
    """Submit ordered user-requested capabilities. Never add approval steps or invent user consent."""
    return {'title': title, 'steps': steps}


def projection(plan):
    value = {key: plan[key] for key in ('id', 'chat_id', 'status', 'title')}
    value['steps'] = [{key: step[key] for key in ('id', 'title', 'status', 'message') if key in step}
                      for step in plan['steps']]
    if plan.get('waiting_prompt_id'):
        value['waiting_prompt_id'] = plan['waiting_prompt_id']
    return value


def plan_part(plan):
    return {'type': 'execution_plan', 'plan_id': plan['id'], 'chat_id': plan['chat_id'],
            'plan': projection(plan)}


def simple_control(body, prompt):
    """Only exact assent or structured UI intent can consume a current approval."""
    prompt = prompt or {}
    command = body.get('command') or {}
    name, args = command.get('name'), command.get('arguments') or command.get('args') or {}
    if name:
        if body.get('reply_to') != prompt.get('id'):
            raise DomainError('确认提示已改变，请查看当前提示后再回复', 409)
        if name == 'clarification.answer' and body.get('reply_kind') == 'clarification':
            return 'answer_clarification_tool', {}, False
        if body.get('reply_kind') != 'confirm':
            return None
        if name in ('artifact.apply', 'artifact.discard') and prompt.get('kind') == 'artifact_proposal':
            for field, actual in (('proposal_id', prompt.get('proposal_id')),
                                  ('artifact_id', prompt.get('artifact_id')),
                                  ('expected_revision', prompt.get('artifact_revision'))):
                if args.get(field) is not None and args[field] != actual:
                    raise DomainError('确认对象或版本与当前预览不一致', 409)
            return ('apply_artifact_preview_tool', {}, False) if name == 'artifact.apply' else (
                'discard_artifact_preview_tool', {}, True)
        if name == 'workflow.resume':
            if args.get('run_id') and args['run_id'] != prompt.get('run_id'):
                raise DomainError('确认任务与当前提示不一致', 409)
            action = args.get('action', command.get('action', 'approved'))
            if action not in ('approved', 'rejected', 'skip_to_cases'):
                raise DomainError('不支持此确认动作')
            return 'resume_pipeline_tool', {'action': action,
                'draft_only': bool(args.get('draft_only', False))}, action == 'rejected'
        return None
    if body.get('reply_kind') in ('question', 'clarification'):
        return None
    text = re.sub(r'[\s。.!！]+$', '', body['content'].strip()).lower()
    approve = text in {'同意', '确认', '接受', '可以', '继续', '好', '好的', '同意继续', '确认继续', '可以，继续',
                       'approve', 'approved', 'accept', 'yes', 'ok', 'continue', '可以，继续吧', '同意，继续',
                       '同意这个修改', '接受这项修改', '同意保存场景修改', '同意保存用例修改', '同意保存需求修改', '评审结果可以，完成吧'}
    approve = approve or (text == '场景同意，生成关联用例' and prompt.get('kind') == 'scenario_review')
    reject = text in {'拒绝', '不同意', '不接受', '取消修改', '拒绝修改', 'reject', 'no'}
    if not (approve or reject) or not prompt.get('id'):
        return None
    if body.get('reply_to') and body['reply_to'] != prompt['id']:
        raise DomainError('确认提示已改变，请查看当前提示后再回复', 409)
    if prompt.get('kind') == 'artifact_proposal':
        return ('discard_artifact_preview_tool' if reject else 'apply_artifact_preview_tool'), {}, reject
    if prompt.get('kind') == 'profile':
        return ('discard_template_tool' if reject else 'apply_profile_tool'), {}, reject
    if text == '场景同意，生成关联用例' and prompt.get('kind') == 'scenario_review':
        return 'resume_pipeline_tool', {'action': 'approved'}, False
    if prompt.get('kind') in {'strategy_review', 'scenario_review', 'case_draft_review', 'case_result_review', 'pause'}:
        return 'resume_pipeline_tool', {'action': 'rejected' if reject else 'approved'}, reject
    return None


class Supervisor:
    def __init__(self, owner):
        self.owner, self.store = owner, owner.store
        self.locks, self.worker = {}, None

    def save(self, plan):
        plan['updated_at'] = now()
        self.store.put('execution_plan', plan)
        # The original chat card is a receipt with a live lightweight projection.
        with suppress(DomainError):
            origin = self.store.get('conversation_turn', plan['turn_id'])
            parts = [p for p in origin.get('parts', []) if not (
                p.get('type') == 'execution_plan' and p.get('plan_id') == plan['id'])]
            origin['parts'] = [*parts, plan_part(plan)]
            self.owner._save(origin)

    def get(self, chat_id, plan_id):
        self.store.get('chat', chat_id)
        plan = self.store.get('execution_plan', plan_id)
        if plan['chat_id'] != chat_id:
            raise DomainError('执行计划不属于当前对话', 404)
        return projection(plan)

    def active(self, chat_id):
        return [p for p in self.store.list('execution_plan', chat_id=chat_id) if p['status'] in ACTIVE]

    async def plan(self, chat, body, prompt, turn):
        context = await chat_context(self.store, self.owner.pipeline, chat, body, prompt)
        # Planning receives metadata and the current user instruction, never source bodies.
        history = sorted(self.store.list('message', chat_id=chat['id']), key=lambda row: row['created_at'])
        context['active_plans'] = [projection(p) for p in self.active(chat['id'])][-3:]
        context['recent_dialogue'] = [{'role': row['role'], 'text': str(row.get('content', ''))[:1000]}
            for row in history if row['id'] not in ('input:' + turn['id'], 'reply:' + turn['id'])][-4:]
        system = load_prompt('planner.system') + '\nCURRENT TRUSTED STATE:\n' + json.dumps(context, ensure_ascii=False)
        model = self.owner.gateway.chat_model().bind_tools([submit_execution_plan], tool_choice='submit_execution_plan')
        diagnostics = getattr(self.owner.gateway, 'diagnostics', None)
        binding = diagnostics.bind(chat_id=chat['id'], project_id=chat['project_id'],
            turn_id=turn['id'], run_id=(prompt or {}).get('run_id'), node='supervisor_planner') if diagnostics else nullcontext()
        with binding:
            response = await model.ainvoke([SystemMessage(content=str(system),
                response_metadata={'tcg_prompt_templates': prompt_templates(system)}),
                HumanMessage(content=body['content'])], tcg_task='supervisor_planner')
        calls = response.tool_calls
        if len(calls) != 1 or calls[0]['name'] != 'submit_execution_plan':
            raise DomainError('模型没有提交有效执行计划；尚未执行任何操作，请重试本次要求')
        try:
            request = PlannedRequest.model_validate(calls[0]['args'])
        except ValueError as exc:
            raise DomainError('执行计划能力或字段不合法；尚未执行任何操作') from exc
        for index, planned_step in enumerate(request.steps):
            if planned_step.capability == 'current_control':
                if index != 0 or not (prompt or {}).get('id') or (prompt or {}).get('kind') in ('busy', 'failed'):
                    raise DomainError('只能处理本轮开始时已展示的确认，不能自动确认后续结果', 409)
                if re.search(r'但是|不过|但|除非|先改|before|except|\bbut\b', body['content'], re.I):
                    raise DomainError('这条回复包含修改或条件，尚未确认；请先说明修改内容', 409)
        if body.get('reply_kind') == 'question' and any(s.capability not in READ_CAPABILITIES for s in request.steps):
            raise DomainError('这条消息是提问，执行计划不能包含修改或流程确认')
        if body.get('reply_kind') == 'clarification' and any(s.capability not in READ_CAPABILITIES | {'pipeline_control', 'clarification'} for s in request.steps):
            raise DomainError('这条消息只提交澄清内容，不能执行其他修改')
        # An acknowledgement embedded in a multi-action request cannot preapprove a later preview.
        bindings = {}
        if body.get('artifact_id'):
            artifact = self.store.get('artifact', body['artifact_id'])
            if artifact['chat_id'] != chat['id']:
                raise DomainError('所选成果不属于当前对话', 404)
            if body.get('artifact_revision') is not None and body['artifact_revision'] != artifact['revision']:
                raise DomainError('所查看的成果版本已改变，请查看当前版本后重新提出要求', 409)
            bindings[artifact['id']] = artifact['revision']
        profile_id = body.get('profile_id') or chat.get('profile_id')
        profile_binding = ({'id': profile_id, 'version': self.store.get('profile', profile_id)['version']}
                           if profile_id else None)
        scope = {key: copy.deepcopy(body[key]) for key in ('artifact_id', 'selected_ids', 'profile_id',
            'source_ids', 'mode', 'reply_kind', 'depth', 'case_types', 'profile_override', 'view_order',
            'artifact_revision', 'as_requirement', 'intent_hint', '_explicit_run_settings') if key in body}
        replacement = next((p for p in self.active(chat['id']) if p['status'] == 'waiting_confirmation'
            and p.get('waiting_prompt_id') == (prompt or {}).get('id')
            and (prompt or {}).get('kind') == 'artifact_proposal'
            and request.steps[0].capability == 'artifact_edit'), None)
        if replacement and (not body.get('artifact_id') or body['artifact_id'] == (prompt or {}).get('artifact_id')):
            for key in ('artifact_id', 'selected_ids', 'profile_id'):
                if key not in scope and key in replacement['_scope']:
                    scope[key] = copy.deepcopy(replacement['_scope'][key])
            bindings.update(replacement['_bindings'])
            profile_binding = copy.deepcopy(replacement.get('_profile_binding'))
        else:
            replacement = None
        value = {'id': uid('plan_'), 'project_id': chat['project_id'], 'chat_id': chat['id'],
            'turn_id': turn['id'], 'title': request.title, 'created_at': now(), 'status': 'running',
            '_scope': scope, '_bindings': bindings, '_profile_binding': profile_binding,
            '_initial_prompt_id': (prompt or {}).get('id'),
            'steps': [{'id': uid('step_'), 'title': step.instruction[:160], 'instruction': step.instruction,
                       'capability': step.capability, 'status': 'pending', 'receipts': [], '_scope': copy.deepcopy(scope)} for step in request.steps]}
        if request.steps[0].capability == 'current_control':
            prior = next((p for p in self.active(chat['id']) if p['status'] == 'waiting_confirmation'
                and p.get('waiting_prompt_id') == (prompt or {}).get('id')), None)
            if prior:
                value['_controls_plan_id'] = prior['id']
                for key in ('artifact_id', 'selected_ids', 'profile_id'):
                    if key not in value['_scope'] and key in prior['_scope']:
                        value['_scope'][key] = copy.deepcopy(prior['_scope'][key])
                value['_bindings'] = copy.deepcopy(prior['_bindings'])
                value['_profile_binding'] = copy.deepcopy(prior.get('_profile_binding'))
                existing = {s['capability'] for s in value['steps'][1:]}
                value['steps'].extend(copy.deepcopy([s for s in prior['steps'] if s['status'] == 'pending'
                                                     and s['capability'] not in existing]))
        if replacement:
            value['_replaces_plan_id'] = replacement['id']
        self.save(value)
        return value

    def _body(self, plan, step, prompt=None):
        original = self.store.get('message', 'input:' + plan['turn_id'])['content']
        body = {**copy.deepcopy(step.get('_scope', plan['_scope'])), 'content': original,
                '_user_request': original, '_turn_id': plan['turn_id'],
                '_plan_id': plan['id'], '_plan_step_id': step['id'], '_expected_revisions': copy.deepcopy(plan['_bindings']),
                '_supervised': True, '_expected_profile': copy.deepcopy(plan.get('_profile_binding'))}
        if body.get('selected_ids'):
            body['_scope_artifact_id'] = body.get('artifact_id')
        if step['capability'] == 'export' and plan.get('_output_artifact_id'):
            if body.get('artifact_id') != plan['_output_artifact_id']:
                body.pop('selected_ids', None)
                body.pop('_scope_artifact_id', None)
            body['artifact_id'] = plan['_output_artifact_id']
            body['_export_artifact_ids'] = [plan['_output_artifact_id']]
        # Only the first observed prompt may be addressed. Later gates require a new user turn.
        body['reply_to'] = plan.get('_initial_prompt_id')
        return body

    def _bind_receipt(self, plan, receipt):
        for part in receipt.get('parts', []):
            if part.get('type') == 'artifact' and part.get('artifact_id') and part.get('revision'):
                plan['_bindings'][part['artifact_id']] = part['revision']
                plan['_output_artifact_id'] = part['artifact_id']
            if part.get('type') == 'diff' and part.get('artifact_id'):
                plan['_output_artifact_id'] = part['artifact_id']
                for change in part.get('changes', []):
                    if change.get('artifact_id') and change.get('expected_revision'):
                        plan['_bindings'][change['artifact_id']] = change['expected_revision']
        profile = receipt.get('profile')
        if isinstance(profile, dict) and profile.get('id') and profile.get('version'):
            plan['_profile_binding'] = {'id': profile['id'], 'version': profile['version']}

    def _check_bindings(self, plan):
        for artifact_id, version in plan['_bindings'].items():
            if self.store.get('artifact', artifact_id)['revision'] != version:
                raise DomainError('计划引用的成果版本已改变，已停止后续操作；请基于当前版本重新提出要求', 409)
        binding = plan.get('_profile_binding')
        if binding and self.store.get('profile', binding['id'])['version'] != binding['version']:
            raise DomainError('计划引用的 Profile 已改变，已停止后续操作；请基于当前模板重新提出要求', 409)

    def _inherit_tail(self, plan, prompt):
        old = self.store.get('execution_plan', plan.pop('_replaces_plan_id'))
        if old['status'] != 'waiting_confirmation' or old.get('waiting_prompt_id') != plan.get('_initial_prompt_id'):
            return
        if prompt.get('artifact_id') != old.get('_waiting_prompt', {}).get('artifact_id'):
            old.update(status='blocked', _block_reason='target_changed')
            for row in old['steps']:
                if row['status'] != 'completed':
                    row.update(status='blocked', message='当前修改目标已改变，原计划后续步骤已暂停。')
            self.save(old)
            return
        existing = {row['capability'] for row in plan['steps'][1:]}
        tail = [copy.deepcopy(row) for row in old['steps'] if row['status'] == 'pending'
                and row['capability'] not in existing]
        plan['steps'].extend(tail)
        old.update(status='cancelled', _superseded_by=plan['id'])
        old.pop('waiting_prompt_id', None)
        for row in old['steps']:
            if row['status'] != 'completed':
                row.update(status='cancelled', message='已替换为新的修改预览；后续步骤保留在新计划中。')
        self.save(old)

    def _receipt_state(self, plan, step):
        receipts = step['receipts']
        if step['capability'] == 'current_control':
            resolved = next((r for r in reversed(receipts) if r.get('tool_name') not in READS
                and r.get('status') in ('succeeded', 'needs_confirmation')), None)
            if resolved and plan.get('_controls_plan_id'):
                prior = self.store.get('execution_plan', plan.pop('_controls_plan_id'))
                prior.update(status='cancelled', _superseded_by=plan['id'])
                prior.pop('waiting_prompt_id', None)
                for row in prior['steps']:
                    if row['status'] != 'completed':
                        row.update(status='cancelled', message='已在本次确认后的计划中继续处理。')
                self.save(prior)
            if resolved and (resolved.get('tool_name') in ('discard_artifact_preview_tool', 'discard_template_tool')
                             or resolved.get('resolution') == 'rejected'):
                step.update(status='completed', message='已拒绝当前修改')
                plan['status'] = 'cancelled'
                for rest in plan['steps']:
                    if rest['status'] == 'pending':
                        rest.update(status='cancelled', message='用户拒绝当前修改，后续操作已取消。')
                return
        # Errors are authoritative even if a later read succeeds.
        error = next((r for r in reversed(receipts) if r.get('status') not in ('succeeded', 'needs_confirmation')), None)
        if error:
            step.update(status='failed', message=error.get('message', '当前子任务未完成'))
            plan['status'] = 'failed'
            return
        pending = next((r for r in reversed(receipts) if r.get('status') == 'needs_confirmation'), None)
        if pending:
            prompt = next(iter(pending.get('pending', [])), {})
            if not prompt.get('id'):
                raise DomainError('修改工具未返回可确认的提示，后续操作已停止')
            if plan.get('_replaces_plan_id'):
                self._inherit_tail(plan, prompt)
            step.update(status='waiting_confirmation', message='等待你查看并接受当前修改；之后自动继续剩余步骤。')
            plan.update(status='waiting_confirmation', waiting_prompt_id=prompt['id'],
                        _waiting_prompt=copy.deepcopy(prompt))
            return
        mutating = [r for r in receipts if r.get('tool_name') not in READS]
        if not mutating and step['capability'] != 'answer':
            step.update(status='failed', message='模型尚未执行本步骤要求的操作；可重试未完成步骤。')
            plan['status'] = 'failed'
            return
        runs = [r.get('run_id') or (r.get('run') or {}).get('id') for r in mutating]
        run_id = next((rid for rid in reversed(runs) if rid), None)
        if run_id and step['capability'] == 'pipeline_start':
            run = self.store.run(run_id)
            if run['status'] != 'completed':
                step.update(status='waiting_pipeline', message='等待后台任务完成；人工确认仍由当前流程逐步处理。')
                plan.update(status='waiting_pipeline', _waiting_run_id=run_id)
                return
        step.update(status='completed', message='已完成')
        plan['status'] = 'running'

    async def execute(self, plan, turn):
        for step in plan['steps']:
            if step['status'] in ('completed', 'cancelled'):
                continue
            if step['status'] != 'pending':
                break
            try:
                self._check_bindings(plan)
            except DomainError as exc:
                step.update(status='blocked', message=str(exc))
                plan.update(status='blocked', _block_reason='version_changed')
                self.save(plan)
                turn.update(status='failed', message=str(exc))
                break
            step['status'] = 'running'
            self.save(plan)
            chat = self.store.get('chat', plan['chat_id'])
            prompt = await current_prompt(self.store, self.owner.pipeline, chat)
            body = self._body(plan, step, prompt)
            allowed = set(CAPABILITIES[step['capability']])
            if step['capability'] == 'current_control':
                if (prompt or {}).get('id') != plan.get('_initial_prompt_id'):
                    message = '当前确认提示已改变，计划没有权限确认新的结果'
                    step.update(status='blocked', message=message)
                    plan.update(status='blocked', _block_reason='prompt_changed')
                    self.save(plan)
                    turn.update(status='needs_input', message=message)
                    break
                kind = (prompt or {}).get('kind')
                allowed = set(READS) | ({'apply_artifact_preview_tool', 'discard_artifact_preview_tool'}
                    if kind == 'artifact_proposal' else {'apply_profile_tool', 'discard_template_tool'}
                    if kind == 'profile' else {'answer_clarification_tool'} if kind == 'clarification'
                    else {'resume_pipeline_tool'})
            if step['capability'] == 'pipeline_control':
                # An ordinary planned step may answer/skip only the original prompt,
                # never automatically approve a freshly produced pipeline gate.
                if (prompt or {}).get('id') != plan.get('_initial_prompt_id'):
                    allowed -= {'resume_pipeline_tool', 'answer_clarification_tool'}
            async def receipt(result):
                item = copy.deepcopy(result)
                # Save compact control receipts, never full read responses/documents.
                if item.get('tool_name') in READS:
                    return
                compact = {key: item[key] for key in ('tool_name', 'status', 'message', 'run_id', 'pending', 'resolution') if key in item}
                compact['parts'] = [{key: p[key] for key in ('type', 'artifact_id', 'revision', 'proposal_id', 'files') if key in p}
                    for p in item.get('parts', []) if p.get('type') in ('artifact', 'diff', 'files')]
                step['receipts'].append(compact)
                self._bind_receipt(plan, item)
                self.save(plan)
            try:
                part_start = len(turn['parts'])
                await self.owner._execute_agent(chat, body, prompt, turn, allowed=allowed,
                    on_receipt=receipt, step=step)
                self._receipt_state(plan, step)
                # A later step replaces the final reply. Preserve an earlier plain
                # answer unless its business tool already produced an answer card.
                if (step['capability'] == 'answer' and step['status'] == 'completed'
                    and any(s['status'] == 'pending' for s in plan['steps'])
                    and not any(p.get('type') == 'answer' for p in turn['parts'][part_start:])
                    and str(turn.get('message', '')).strip()):
                    turn['parts'].append({'type': 'assistant_note', 'text': turn['message']})
            except BaseException as exc:
                if step['receipts']:
                    self._receipt_state(plan, step)
                elif isinstance(exc, asyncio.CancelledError) and step['capability'] not in READ_CAPABILITIES:
                    step.update(status='blocked', message='操作中断时结果尚未确认，请查看成果后重新提出未完成要求。')
                    plan.update(status='blocked', _block_reason='uncertain_effect')
                else:
                    step.update(status='failed', message='当前步骤未完成；已有步骤不会重复执行。')
                    plan['status'] = 'failed'
                self.save(plan)
                raise
            self.save(plan)
            if plan['status'] != 'running':
                break
        if plan['status'] != 'cancelled' and all(s['status'] in ('completed', 'cancelled') for s in plan['steps']):
            plan['status'] = 'completed'
            self.save(plan)
        turn['parts'] = [p for p in turn['parts'] if not (p.get('type') == 'execution_plan' and p.get('plan_id') == plan['id'])]
        turn['parts'].append(plan_part(plan))
        if plan['status'] == 'waiting_confirmation':
            turn['status'] = 'needs_confirmation'
            turn['message'] = '修改预览已准备好。接受后会自动继续后续步骤；拒绝则取消剩余步骤。'
        elif plan['status'] == 'waiting_pipeline':
            turn['status'] = 'succeeded'
            turn['message'] = '后台任务已启动，完成后会继续执行计划中的后续操作；人工确认节点仍需你逐步回复。'
        elif plan['status'] in ('failed', 'blocked'):
            if turn.get('status') != 'needs_input':
                turn['status'] = 'failed'
        return self.owner._save(turn)

    async def after_control(self, chat_id, prompt, turn, rejected=False, applied=None):
        matched = False
        for plan in self.active(chat_id):
            if plan['status'] != 'waiting_confirmation' or plan.get('waiting_prompt_id') != (prompt or {}).get('id'):
                continue
            matched = True
            async with self.locks.setdefault(plan['id'], asyncio.Lock()):
                plan = self.store.get('execution_plan', plan['id'])
                if plan['status'] != 'waiting_confirmation':
                    continue
                step = next(s for s in plan['steps'] if s['status'] == 'waiting_confirmation')
                if rejected:
                    plan['status'] = 'cancelled'
                    for remaining in plan['steps']:
                        if remaining['status'] != 'completed':
                            remaining.update(status='cancelled', message='用户拒绝当前修改，后续操作已取消。')
                    plan.pop('waiting_prompt_id', None)
                    self.save(plan)
                    turn['parts'].append(plan_part(plan))
                    continue
                receipts = ([applied] if applied else [a['result'] for a in turn.get('actions', [])])
                if not receipts or any(r.get('status') not in (None, 'succeeded', 'needs_confirmation') for r in receipts):
                    continue
                skipped = next((r.get('skipped_keys') for r in receipts if r.get('skipped_keys')), None)
                if not skipped and (prompt or {}).get('kind') == 'profile':
                    for tid in (prompt or {}).get('template_ids', []):
                        template = self.store.get('template', tid)
                        skipped = skipped or template.get('_skipped_keys')
                if skipped:
                    plan.update(status='blocked', _block_reason='partial_approval')
                    step.update(status='blocked', message='只采用了部分 Profile 更改，后续导出已暂停；请说明使用当前模板还是继续调整。')
                    self.save(plan)
                    turn['parts'].append(plan_part(plan))
                    continue
                for result in receipts:
                    self._bind_receipt(plan, result)
                # Column edits may need a separately reviewed Profile change.
                followup = next((p for r in receipts for p in r.get('pending', []) if p.get('kind') == 'profile'), None)
                if followup:
                    plan.update(waiting_prompt_id=followup['id'], _waiting_prompt=copy.deepcopy(followup))
                    self.save(plan)
                    turn['parts'].append(plan_part(plan))
                    continue
                if applied and (applied.get('status') not in (None, 'succeeded')):
                    continue
                step.update(status='completed', message='已接受并保存修改')
                plan['status'] = 'running'
                plan.pop('waiting_prompt_id', None)
                plan.pop('_waiting_prompt', None)
                # Older case-column deferred exports may have produced the exact
                # artifact already. Treat its durable receipt as the export step.
                files = [f for r in receipts for p in r.get('parts', []) if p.get('type') == 'files' for f in p.get('files', [])]
                if files:
                    for remaining in plan['steps']:
                        if remaining['status'] == 'pending' and remaining['capability'] == 'export':
                            remaining.update(status='completed', message='已导出批准后的版本')
                self.save(plan)
                await self.execute(plan, turn)
        return self.owner._save(turn) if matched else self.owner.response(turn)

    def cancel_tail(self, chat_id, turn, *, exports_only=False):
        changed = []
        for plan in self.active(chat_id):
            touched = False
            for step in plan['steps']:
                if step['status'] == 'completed' or (exports_only and step['capability'] != 'export'):
                    continue
                if step['status'] == 'running':
                    raise DomainError('当前操作已经执行中；请等待本步结果，后续操作可以取消', 409)
                step.update(status='cancelled', message='已按你的要求取消。')
                touched = True
            if touched:
                if not exports_only or all(s['status'] in ('completed', 'cancelled') for s in plan['steps']):
                    plan['status'] = 'cancelled' if not exports_only else 'completed'
                    plan.pop('waiting_prompt_id', None)
                self.save(plan)
                changed.append(plan_part(plan))
        turn.update(status='succeeded', message=('已取消后续导出。当前修改预览仍可单独接受或拒绝。' if exports_only
            else '已取消尚未执行的计划步骤。已有成果和当前修改预览保留。') if changed else '当前没有可取消的后续计划步骤。')
        turn['parts'].extend(changed)
        return self.owner._save(turn)

    async def retry(self, chat, turn):
        plans = [p for p in self.active(chat['id']) if p['status'] in ('failed', 'blocked')]
        if len(plans) != 1:
            raise DomainError('请说明需要重试的执行计划；当前没有唯一的失败计划')
        plan = plans[0]
        async with self.locks.setdefault(plan['id'], asyncio.Lock()):
            if plan.get('_block_reason') == 'uncertain_effect':
                raise DomainError('服务中断时操作结果未确定，不能自动重放；请查看已有成果后重新提出尚未完成的要求', 409)
            self._check_bindings(plan)
            for step in plan['steps']:
                if step['status'] in ('failed', 'blocked'):
                    if any(r.get('status') == 'succeeded' for r in step['receipts']):
                        step['status'] = 'completed'
                    else:
                        step.update(status='pending', receipts=[])
            plan['status'] = 'running'
            self.save(plan)
            return await self.execute(plan, turn)

    async def recover(self):
        from .supervisor_recovery import recover_plans, watch_plans
        recover_plans(self)
        self.worker = asyncio.create_task(watch_plans(self))

    async def close(self):
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
