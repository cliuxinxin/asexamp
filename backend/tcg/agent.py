"""Versioned LangGraph agent. Durable instruction epochs guard every accepted output.

Only concise, evidence-cited findings are exposed as analysis. Planner decisions
are structured actions, never fabricated execution or private reasoning traces.
"""
import json
import re
from collections import Counter
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import agent_contracts as contract
from .documents import parse_text
from .schemas import DomainError, OutputValidationError, INTENTS, apply_operations, profile_config, validate_items
from .storage import now, public, uid


class AgentState(TypedDict, total=False):
    run_id: str
    epoch: int
    iteration: int
    action: str
    analysis_ref: str | None
    scenario_ref: str | None
    cases_ref: str | None
    checked: bool
    approved: bool
    repairs: int
    no_progress: int
    signature: str
    stale: bool
    done: bool
    output_ref: str | None
    intent: str
    reassessments: int


class StaleInstruction(Exception):
    pass


class IncompleteCoverage(DomainError):
    category = 'incomplete_coverage'


class Agent:
    MAX_ITERATIONS = 18
    MAX_REPAIRS = 3

    def __init__(self, engine, saver):
        self.engine, self.store = engine, engine.store
        builder = StateGraph(AgentState)
        for name in ('plan', 'work', 'gate', 'finish'):
            builder.add_node(name, self.observed(name))
        builder.add_edge(START, 'plan')
        builder.add_conditional_edges('plan', lambda s: 'plan' if s.get('stale') else 'finish' if s['action'] == 'finish' else 'work')
        builder.add_conditional_edges('work', lambda s: 'gate' if s.get('analysis_ref') and not s.get('approved') and not s.get('stale') else 'plan')
        builder.add_edge('gate', 'plan')
        builder.add_conditional_edges('finish', lambda s: END if s.get('done') else 'plan')
        self.graph = builder.compile(checkpointer=saver)

    def observed(self, name):
        async def node(state):
            with self.engine.diagnostics.bind(node='agent_' + name):
                self.engine.trace('node.start', state['run_id'])
                try:
                    result = await getattr(self, name)(state)
                except StaleInstruction:
                    self.engine.trace('agent.obsolete_result_discarded', state['run_id'])
                    return {'stale': True}
                self.engine.trace('node.complete', state['run_id'])
                return result
        return node

    def current(self, state):
        self.store.assert_running(state['run_id'])
        run = self.store.run(state['run_id'])
        if run.get('_instruction_version', 0) != state.get('epoch', 0):
            raise StaleInstruction()
        return run

    def update(self, run_id, **changes):
        with self.store.transaction():
            run = self.store.run(run_id)
            agent = {**run.get('agent', {}), **changes}
            self.store.update_run(run_id, agent=agent)
            self.store.append_event(run_id, 'agent', {'run_id': run_id, 'agent': agent})

    def insight(self, run_id, summary, refs, kind='finding'):
        agent = self.store.run(run_id)['agent']
        self.update(run_id, insights=agent.get('insights', []) + [{'id': uid('ins_'), 'summary': summary, 'refs': refs, 'kind': kind}])

    def context(self, state, **extra):
        run = self.store.run(state['run_id'])
        depth = run.get('agent', {}).get('depth', 'standard')
        memory = self.store.get('chat', run['chat_id']).get('memory', {})
        context = self.engine.context(state['run_id'], memory=memory, instructions=run.get('_instructions', []), depth=depth,
                                      requested_depth=run['_request'].get('depth', 'auto'), depth_guidance=contract.DEPTH_GUIDANCE.get(depth, contract.DEPTH_GUIDANCE['standard']))
        context['request'].update(experience='agent', confirm_strategy=run['_request'].get('confirm_strategy', True))
        for key, name in (('analysis_ref', 'analysis'), ('scenario_ref', 'scenarios'), ('cases_ref', 'cases')):
            if state.get(key):
                artifact = self.store.get('artifact', state[key])
                context[name] = artifact['items']
                if name == 'analysis':
                    context['business_model'] = artifact['report']['business_model']
                    context['strategy'] = artifact['report']['strategy']
                    context['assumptions'] = artifact['report'].get('assumptions', [])
                    context['deferred_questions'] = artifact['report'].get('deferred_questions', [])
        decision = self.continuation(run)
        if decision:
            context['clarification_decision'] = decision
        context.update(extra)
        return context

    @staticmethod
    def continuation(run):
        """A decision only applies until the next business instruction."""
        decision = run.get('_clarification_decision')
        return decision if decision and decision['epoch'] == run.get('_instruction_version', 0) else None

    @staticmethod
    def is_continue_command(content):
        # Match whole short commands only; negations and mixed business replies
        # go through semantic classification instead of a substring heuristic.
        normalized = re.sub(r'[\s，,。.!！?？；;、]', '', content).lower()
        return normalized in {
            '不要对了就这样吧', '不要再对了就这样吧', '别问了直接生成',
            '不用再确认了直接生成', '就这样吧', '就这样继续', '按当前信息继续',
            '按现有信息继续', '按照一般的系统进行假设', '按一般系统假设继续',
            '确认继续', '确认', '继续', '直接生成', '继续生成用例',
            'proceed', 'continue', 'proceedwithcurrentinformation',
        }

    async def feedback(self, state, response, analysis):
        content = (response.get('answer') or '').strip()
        if self.is_continue_command(content):
            return {'proceed': True, 'has_changes': False}
        if response.get('proceed') and not content:
            return {'proceed': True, 'has_changes': False}
        def validate(result):
            contract.require(type(result.get('proceed')) is bool and type(result.get('has_changes')) is bool,
                             'feedback', 'boolean_proceed_and_has_changes')
            return result
        result = await self.call(state, 'agent_feedback', self.context(state, feedback=content,
            questions=analysis['report'].get('questions', [])), validate)
        return {**result, 'proceed': bool(response.get('proceed')) or result['proceed']}

    async def call(self, state, task, context, validator):
        key = f'agent:{state["epoch"]}:{state["iteration"]}:{task}'
        result = await self.engine.call(state['run_id'], key, task, context)
        original = result
        self.current(state)
        for attempt in range(2):
            try:
                accepted = validator(result)
                if attempt and task in ('agent_analyze', 'agent_scenarios', 'agent_cases', 'import_cases'):
                    self.preserve_schema_repair(original, accepted)
                return accepted
            except OutputValidationError as exc:
                if attempt:
                    raise
                result = await self.engine.call(state['run_id'], key + ':schema_repair', task,
                    {**context, 'validation_repair': {'validation_error': exc.issue, 'previous_response': result}})
                self.current(state)

    @staticmethod
    def preserve_schema_repair(original, corrected):
        """Schema repairs may fix invalid fields, never erase previously generated items."""
        if isinstance(original.get('items'), list):
            contract.require(len(original['items']) == len(corrected['items']), 'items', 'same_schema_repair_item_count')
            ids = Counter(i.get('id') for i in original['items'] if isinstance(i, dict) and isinstance(i.get('id'), str))
            retained = {i['id'] for i in corrected['items']}
            contract.require(all(i in retained for i, count in ids.items() if i and len(i) <= 200 and count == 1), 'items.id', 'preserved_valid_stable_ids')
        if isinstance(original.get('has_more'), bool):
            contract.require(original['has_more'] == corrected.get('has_more'), 'has_more', 'unchanged_page_continuation')
        graph = original.get('report', {}).get('business_model') if isinstance(original.get('report'), dict) else None
        if isinstance(graph, dict):
            for field in ('nodes', 'edges'):
                if isinstance(graph.get(field), list):
                    old = {i['id'] for i in graph[field] if isinstance(i, dict) and isinstance(i.get('id'), str)}
                    new = {i['id'] for i in corrected['report']['business_model'][field]}
                    contract.require(old <= new, 'business_model.' + field, 'preserved_graph_ids_during_schema_repair')

    def available(self, state):
        intent = state.get('intent')
        if state.get('output_ref'):
            return ['finish']
        if intent in ('query', 'modify', 'learn_template'):
            return [intent]
        if intent == 'review_case':
            run = self.store.run(state['run_id'])
            return ['review_cases'] if state.get('cases_ref') or run.get('_artifact_snapshot') else ['import_cases']
        if not state.get('analysis_ref'):
            return ['analyze']
        if intent == 'review_requirement':
            return ['finish', 'analyze']
        if not state.get('scenario_ref'):
            return ['scenarios', 'analyze']
        if not state.get('cases_ref') and intent != 'generate_scenario':
            return ['cases', 'repair_scenarios', 'analyze']
        if not state.get('checked'):
            return ['check', 'repair_scenarios', 'analyze'] + (['repair_cases'] if intent != 'generate_scenario' else [])
        coverage = self.store.run(state['run_id'])['agent']['coverage']
        if not coverage['gaps']:
            return ['finish', 'analyze', 'repair_scenarios'] + (['repair_cases'] if intent != 'generate_scenario' else [])
        scenarios = self.store.get('artifact', state['scenario_ref'])['items']
        scenario_reqs = {r for s in scenarios for r in s['requirement_ids']}
        scenario_branches = {b for s in scenarios for b in s['branch_ids']}
        missing_scenarios = any(g['kind'] == 'requirement' and g['id'] not in scenario_reqs or g['kind'] == 'branch' and g['id'] not in scenario_branches for g in coverage['gaps'])
        return ['repair_scenarios', 'analyze'] if missing_scenarios or intent == 'generate_scenario' else ['repair_cases', 'repair_scenarios', 'analyze']

    async def plan(self, state):
        run = self.store.run(state['run_id'])
        epoch = run.get('_instruction_version', 0)
        changes = {}
        if state.get('epoch') != epoch or state.get('stale'):
            changes = {'epoch': epoch, 'analysis_ref': None, 'scenario_ref': None, 'cases_ref': None, 'approved': False,
                       'checked': False, 'repairs': 0, 'no_progress': 0, 'signature': '', 'stale': False, 'output_ref': None}
            changes['reassessments'] = 0
            state = {**state, **changes}
            with self.store.transaction():
                self.store.update_run(run['id'], _applied_instruction_version=epoch)
                self.update(run['id'], pending_instructions=0)
        iteration = state.get('iteration', 0) + 1
        if iteration > self.MAX_ITERATIONS:
            raise IncompleteCoverage('已达到规划迭代预算，任务未完成。已有策略和版本已保留；请缩小范围或补充需求后重试。')
        state = {**state, 'iteration': iteration}
        self.engine.stage(run['id'], 'planning')
        if not state.get('intent') or state['intent'] == 'auto':
            intent = run['_request']['intent']
            if intent == 'auto':
                route_context = self.engine.routing_context(run['id'])
                route_context['confirmed_memory'] = self.store.get('chat', run['chat_id']).get('memory', {})
                routed = await self.call(state, 'route', route_context, self.route_contract)
                intent = routed['intent']
            source_ids = run['_source_ids']
            if intent in ('query', 'modify', 'review_case'):
                source_ids = list(dict.fromkeys(source_ids + run.get('_artifact_source_ids', [])))
            self.store.update_run(run['id'], intent=intent, _source_ids=source_ids)
            changes['intent'] = intent
            state['intent'] = intent
        available = self.available(state)
        if self.continuation(run) and state.get('analysis_ref'):
            available = [action for action in available if action != 'analyze']
        context = self.context(state, available_actions=available, coverage=run.get('agent', {}).get('coverage'), iteration=iteration, max_iterations=self.MAX_ITERATIONS)
        evidence = {e['id']: e for e in context['evidence']}
        def validator(result):
            contract.plan(result, available, evidence, run['_request'].get('depth', 'auto'), allow_examples=state['intent'] == 'learn_template')
            if state.get('analysis_ref') and result['next_action'] != 'analyze':
                contract.require(result['depth'] == context['strategy']['depth'], 'depth', 'confirmed_strategy_depth_or_reassess_analysis')
            return result
        result = await self.call(state, 'agent_plan', context, validator)
        with self.store.transaction():
            self.current(state)
            completed = {'analyze': state.get('approved'), 'scenarios': state.get('scenario_ref'), 'cases': state.get('cases_ref'), 'check': state.get('checked')}
            steps = [{**s, 'status': 'running' if s['id'] == result['next_action'].removeprefix('repair_') else 'completed' if completed.get(s['id']) else 'pending'} for s in result['plan']]
            self.update(run['id'], depth=result['depth'], rationale=result['rationale'], plan=steps)
            self.insight(run['id'], result['insight']['summary'], result['insight']['refs'])
        return {**changes, 'iteration': iteration, 'action': result['next_action'], 'stale': False}

    @staticmethod
    def route_contract(result):
        contract.require(result.get('intent') in INTENTS, 'intent', 'supported_intent')
        return result

    async def work(self, state):
        self.current(state)
        run_id, action = state['run_id'], state['action']
        self.engine.stage(run_id, {'analyze': 'requirement_analysis', 'scenarios': 'scenario_generation', 'cases': 'case_generation', 'check': 'coverage_check'}.get(action, 'coverage_repair'))
        context = self.context(state)
        evidence = {e['id']: e for e in context['evidence']}
        if action in ('query', 'modify', 'review_cases', 'learn_template', 'import_cases'):
            return await self.single(state, action, context, evidence)
        if action == 'analyze':
            if not any(e['role'] != 'example' for e in evidence.values()):
                def intake_schema(result):
                    contract.require(result.get('classification') in ('requirement', 'instruction', 'ambiguous'), 'classification', 'requirement|instruction|ambiguous')
                    contract.text(result.get('question'), 'question')
                    return result
                intake = await self.call(state, 'agent_intake', context, intake_schema)
                if intake['classification'] == 'requirement':
                    with self.store.transaction():
                        run = self.current(state)
                        text, chunks = parse_text(run['_request']['content'])
                        source = self.store.add_source(run['chat_id'], '当前消息中的业务规则', 'primary', text, chunks)
                        self.store.update_run(run_id, _source_ids=run['_source_ids'] + [source['id']])
                        self.insight(run_id, '将当前消息中的明确业务规则作为需求来源；请在策略确认时检查识别结果。', [e['id'] for e in self.store.evidence([source['id']])])
                    context = self.context(state)
                    evidence = {e['id']: e for e in context['evidence']}
                else:
                    answer = interrupt({'type': 'clarification', 'questions': [intake['question']]})
                    if not answer.get('instruction'):
                        self.add_instruction(run_id, answer.get('answer', ''), resume=False)
                    return {'stale': True}
            result = await self.call(state, 'agent_analyze', context, lambda r: contract.analysis(r, evidence, context['depth']))
            reassessments = state.get('reassessments', 0)
            if state.get('analysis_ref'):
                old = self.store.get('artifact', state['analysis_ref'])
                contract.require({i['id'] for i in old['items']} <= {i['id'] for i in result['items']}, 'items.id', 'preserve_confirmed_requirement_ids_until_user_changes_scope')
                contract.require({e['id'] for e in old['report']['business_model']['edges']} <= {e['id'] for e in result['report']['business_model']['edges']}, 'business_model.edges', 'preserve_confirmed_branch_ids_until_user_changes_scope')
                unchanged = old['items'] == result['items'] and old['report']['business_model'] == result['report']['business_model']
                reassessments = reassessments + 1 if unchanged else 0
                if reassessments >= 2:
                    raise IncompleteCoverage('重复分析未取得新进展，已停止规划循环。请补充新的业务证据或调整范围后重试。')
            with self.store.transaction():
                self.current(state)
                self.remember(run_id, {'items': result['items'], 'report': result['report']}, confirmed=False)
                self.supersede(run_id, result['report'].get('conflicts', []), evidence)
                artifact = self.store.artifact(run_id, f'agent_analysis:{state["epoch"]}:{state["iteration"]}', 'analysis', '需求分析与测试策略', result['items'], result['report'])
                self.insight(run_id, result['report'].get('summary') or result['report']['strategy']['rationale'], list(dict.fromkeys(ref for item in result['items'] for ref in item['refs'])))
            return {'analysis_ref': artifact['id'], 'approved': False, 'scenario_ref': None, 'cases_ref': None, 'checked': False, 'reassessments': reassessments}
        if action == 'check':
            scenario_goal = state['intent'] == 'generate_scenario'
            coverage = contract.coverage(context['analysis'], context['business_model'], context['scenarios'],
                [{**s, 'scenario_id': s['id']} for s in context['scenarios']] if scenario_goal else context['cases'])
            signature = json.dumps(coverage['gaps'], sort_keys=True)
            no_progress = state.get('no_progress', 0) + 1 if signature == state.get('signature') else 0
            with self.store.transaction():
                self.current(state)
                self.update(run_id, coverage=coverage)
                self.insight(run_id, f'设计覆盖：需求 {coverage["requirements_covered"]}/{coverage["requirements_total"]}，分支 {coverage["branches_covered"]}/{coverage["branches_total"]}；未执行测试。', list(dict.fromkeys(r for i in context['analysis'] for r in i['refs'])))
            if coverage['gaps'] and (state.get('repairs', 0) >= self.MAX_REPAIRS or no_progress >= 2):
                raise IncompleteCoverage('覆盖修复未取得足够进展，设计仍有缺口，未发布最终用例。已有分析和检查结果已保留；请补充规则或调整范围后重试。')
            return {'checked': True, 'signature': signature, 'no_progress': no_progress}
        kind = 'scenarios' if action in ('scenarios', 'repair_scenarios') else 'cases'
        if action.startswith('repair_') and state.get('repairs', 0) >= self.MAX_REPAIRS:
            raise IncompleteCoverage('达到定向修复预算，未发布不完整结果。请检查问题并补充规则后重试。')
        previous = context.get(kind, []) if action.startswith('repair_') else []
        items, cursor, used = list(previous), None, set()
        for page in range(200):
            page_context = {**context, 'previous_items': items, 'cursor': cursor, 'coverage': self.store.run(run_id)['agent'].get('coverage'), 'repair': action.startswith('repair_')}
            page_state = {**state, 'iteration': f'{state["iteration"]}:{page}'}
            result = await self.call(page_state, 'agent_' + kind, page_context,
                lambda r: contract.generated(r, kind, evidence, context['analysis'], context['business_model'], context.get('scenarios') if kind == 'cases' else None))
            new = result['items']
            contract.require(not {i['id'] for i in items}.intersection(i['id'] for i in new), 'items.id', 'new_unique_ids')
            items.extend(new)
            if not result['has_more']:
                break
            next_cursor = result.get('next_cursor')
            contract.require(bool(new) and isinstance(next_cursor, str) and next_cursor and next_cursor not in used, 'next_cursor', 'new_cursor_and_new_items')
            cursor = next_cursor
            used.add(cursor)
        else:
            raise IncompleteCoverage('分页达到执行预算；未接受不完整结果，请拆分范围后重试。')
        with self.store.transaction():
            self.current(state)
            artifact = self.store.artifact(run_id, f'agent_{kind}:{state["epoch"]}:{state["iteration"]}', kind, '测试场景' if kind == 'scenarios' else '测试用例', items,
                {'strategy': context['strategy'], 'business_model': context['business_model'], 'requirements': context['analysis'],
                 'assumptions': context.get('assumptions', []), 'deferred_questions': context.get('deferred_questions', []),
                 **({'clarification_decision': context['clarification_decision']} if context.get('clarification_decision') else {}),
                 **({'scenarios': context['scenarios']} if kind == 'cases' else {}), 'traceability': [
                    {**{k: i[k] for k in ('id', 'title', 'requirement_ids', 'branch_ids')}, 'scenario_id': i.get('scenario_id', i['id']),
                     **({'case_id': i['id']} if kind == 'cases' else {}), 'refs': i['refs']} for i in items]})
        return {('scenario_ref' if kind == 'scenarios' else 'cases_ref'): artifact['id'], 'checked': False,
                'repairs': state.get('repairs', 0) + int(action.startswith('repair_'))}

    async def single(self, state, action, context, evidence):
        run_id = state['run_id']
        run = self.current(state)
        if action == 'query':
            if not any(e['role'] != 'example' for e in evidence.values()):
                response = interrupt({'type': 'clarification', 'questions': ['请提供回答问题所需的需求来源或业务规则。']})
                if not response.get('instruction'):
                    self.add_instruction(run_id, response.get('answer', ''), resume=False)
                return {'stale': True}
            def validator(result):
                contract.text(result.get('answer'), 'answer')
                contract.refs(result.get('refs'), evidence, 'answer.refs')
                return result
            result = await self.call(state, 'query', context, validator)
            with self.store.transaction():
                self.current(state)
                artifact = self.engine.answer(run_id, f'agent_answer:{state["epoch"]}', result['answer'], result['refs'])
            return {'output_ref': artifact['id']}
        if action == 'learn_template':
            if not context['evidence'] and not context['artifact']:
                raise DomainError('请上传格式样例或选择已有 Artifact 后学习模板；未生成无依据的配置。')
            def validator(result):
                result['config'] = profile_config(result.get('config'))
                contract.text(result.get('summary'), 'summary')
                return result
            result = await self.call(state, 'learn_template', context, validator)
            with self.store.transaction():
                self.current(state)
                artifact = self.store.artifact(run_id, f'agent_proposal:{state["epoch"]}', 'proposal', 'Profile 配置建议',
                    [{'id': uid('proposal_'), 'title': '模板学习建议', 'description': result['summary'], 'refs': []}], {'config': result['config']})
            return {'output_ref': artifact['id']}
        if action == 'import_cases':
            items, cursor, used = [], None, set()
            for page in range(200):
                def validator(result):
                    validate_items('cases', result.get('items'), evidence)
                    contract.require(isinstance(result.get('has_more'), bool), 'has_more', 'boolean')
                    return result
                result = await self.call({**state, 'iteration': f'{state["iteration"]}:{page}'}, 'import_cases', {**context, 'previous_items': items, 'cursor': cursor}, validator)
                contract.require(not {i['id'] for i in items}.intersection(i['id'] for i in result['items']), 'items.id', 'new_unique_ids')
                items += result['items']
                if not result['has_more']:
                    break
                cursor = result.get('next_cursor')
                contract.require(bool(result['items']) and isinstance(cursor, str) and cursor and cursor not in used, 'next_cursor', 'new_cursor_and_new_items')
                used.add(cursor)
            else:
                raise IncompleteCoverage('导入分页达到执行预算，未接受不完整结果。')
            contract.require(bool(items), 'items', 'nonempty_imported_cases')
            with self.store.transaction():
                self.current(state)
                artifact = self.store.artifact(run_id, f'agent_import:{state["epoch"]}', 'cases', '导入用例', items)
            return {'cases_ref': artifact['id']}
        snapshot = self.store.get('artifact', state['cases_ref']) if state.get('cases_ref') else run.get('_artifact_snapshot')
        if not snapshot:
            raise DomainError('请先选择需要修改或评审的 Artifact，再发送指令。')
        if action == 'review_cases' and snapshot['type'] != 'cases':
            raise DomainError('用例评审需要测试用例 Artifact；请选择用例后重试。')
        context = {**context, 'artifact': public(snapshot), 'cases': snapshot['items'] if action == 'review_cases' else context.get('cases', [])}
        def validator(result):
            items = apply_operations(snapshot['items'], result.get('operations'), run['_request'].get('selected_ids'))
            validate_items(snapshot['type'], items, evidence)
            result['_accepted_items'] = items
            return result
        result = await self.call(state, 'modify' if action == 'modify' else 'review_cases', context, validator)
        with self.store.transaction():
            self.current(state)
            report = contract.refreshed_report(snapshot['type'], result['_accepted_items'], snapshot.get('report', {}), evidence)
            if action == 'review_cases':
                report['review'] = result.get('report', {})
            artifact = self.store.artifact(run_id, f'agent_edit_candidate:{state["epoch"]}:{state["iteration"]}', snapshot['type'], snapshot['title'], result['_accepted_items'], report)
            artifact['_agent_target'] = {'id': snapshot['id'], 'revision': snapshot['revision'], 'reason': 'ai_agent_modify' if action == 'modify' else 'ai_agent_review'}
            self.store.put('artifact', artifact)
            if report.get('coverage'):
                self.update(run_id, coverage=report['coverage'])
        return {'output_ref': artifact['id']}

    async def gate(self, state):
        run = self.current(state)
        analysis = self.store.get('artifact', state['analysis_ref'])
        questions = analysis['report'].get('questions', [])
        needs_review = run['_request'].get('confirm_strategy', True)
        decision = self.continuation(run)
        if not decision and (questions or needs_review):
            self.store.publish(run['id'], [analysis['id']], '有些信息尚未明确。你可以补充，也可以按当前信息继续，未决问题会保留在结果中。' if questions else '请确认业务模型、范围与测试策略；也可以发送补充指令修订。', waiting=True)
            response = interrupt({'type': 'clarification' if questions else 'strategy_review', 'artifact_id': analysis['id'], 'questions': questions, 'can_proceed': True})
            if response.get('instruction'):
                return {'stale': True}
            if response.get('proceed') or (response.get('answer') or '').strip():
                feedback = await self.feedback(state, response, analysis)
                with self.store.transaction():
                    self.current(state)
                    if feedback['has_changes'] or not feedback['proceed']:
                        self.add_instruction(run['id'], response.get('answer', ''), resume=False)
                        self.store.update_run(run['id'], _gate_feedback_pending=False)
                        if feedback['proceed']:
                            updated = self.store.run(run['id'])
                            self.store.update_run(run['id'], _clarification_decision={
                                'mode': 'proceed', 'epoch': updated['_instruction_version'],
                                'content': response['answer'], 'instruction_recorded': True})
                        return {'stale': True}
                    decision = {'mode': 'proceed', 'epoch': state['epoch'],
                                'content': response.get('answer') or '按当前信息继续', 'instruction_recorded': False}
                    self.store.update_run(run['id'], _clarification_decision=decision)
            elif questions or response.get('approved') is not True:
                raise DomainError('请确认策略后继续，或发送补充指令修改策略。')
        with self.store.transaction():
            self.current(state)
            self.store.update_run(run['id'], _gate_feedback_pending=False)
            if decision:
                decision = {**decision, 'deferred_questions': questions, 'assumptions': analysis['report'].get('assumptions', [])}
                self.store.update_run(run['id'], _clarification_decision=decision)
                # A separate artifact preserves the originally reviewed version.
                analysis = self.store.artifact(run['id'], f'agent_continued_analysis:{state["epoch"]}:{state["iteration"]}',
                    'analysis', analysis['title'], analysis['items'], {**analysis['report'],
                    'deferred_questions': questions, 'clarification_decision': decision})
                if not decision.get('instruction_recorded'):
                    self.store.put('message', {'id': f'{run["id"]}:continue:{state["epoch"]}', 'chat_id': run['chat_id'],
                        'project_id': run['project_id'], 'role': 'user', 'content': decision['content'],
                        'created_at': now(), 'metadata': {'run_id': run['id'], 'workflow_decision': True}})
                self.insight(run['id'], '按你的指示继续设计；未决问题和假设会保留，不会当作已确认的业务规则。', [], 'decision')
            self.remember(run['id'], analysis, confirmed=needs_review or bool(decision))
            if not decision:
                self.insight(run['id'], '测试策略已确认。' if needs_review else '按自动推进设置采用当前策略；模型假设仍未确认为需求。', list(dict.fromkeys(r for i in analysis['items'] for r in i['refs'])), 'decision')
        return {'approved': True, 'analysis_ref': analysis['id']}

    def supersede(self, run_id, conflicts, evidence):
        contract.require(isinstance(conflicts, list), 'conflicts', 'array')
        run = self.store.run(run_id)
        chat = self.store.get('chat', run['chat_id'])
        memory = chat.get('memory', {'decisions': []})
        decisions = {d['id']: d for d in memory['decisions']}
        for conflict in conflicts:
            contract.require(isinstance(conflict, dict) and conflict.get('decision_id') in decisions, 'conflicts.decision_id', 'existing_confirmed_decision')
            contract.text(conflict.get('summary'), 'conflicts.summary')
            contract.refs(conflict.get('refs'), evidence, 'conflicts.refs')
            contract.require(all(evidence[r]['role'] in ('change', 'clarification') for r in conflict['refs']), 'conflicts.refs', 'new_change_or_user_clarification')
            decision = decisions[conflict['decision_id']]
            contract.require(not set(conflict['refs']).issubset(decision['refs']), 'conflicts.refs', 'newer_evidence')
            decision.update(status='superseded', superseded_by=conflict['refs'], supersession=conflict['summary'])
            self.insight(run_id, conflict['summary'], conflict['refs'], 'decision')
        if conflicts:
            memory['updated_at'] = now()
            chat['memory'] = memory
            self.store.put('chat', chat)

    def remember(self, run_id, analysis, confirmed):
        run = self.store.run(run_id)
        chat = self.store.get('chat', run['chat_id'])
        memory = chat.get('memory', {'decisions': [], 'scope': [], 'open_questions': [], 'source_refs': []})
        memory['scope'] = analysis['report']['strategy']['scope']
        memory['scope_confirmed'] = confirmed
        memory['open_questions'] = analysis['report'].get('questions', [])
        memory['clarification_decision'] = self.continuation(run)
        memory['assumptions'] = analysis['report'].get('assumptions', [])
        memory['source_refs'] = list(dict.fromkeys(memory.get('source_refs', []) + [r for i in analysis['items'] for r in i['refs']]))
        key = run_id + ':' + str(run.get('_instruction_version', 0))
        if confirmed and not any(d['id'] == key for d in memory['decisions']):
            memory['decisions'].append({'id': key, 'summary': '确认设计范围：' + '、'.join(memory['scope']), 'refs': [r for i in analysis['items'] for r in i['refs']], 'status': 'confirmed', 'run_id': run_id})
        memory['updated_at'] = now()
        chat['memory'] = memory
        self.store.put('chat', chat)

    def add_instruction(self, run_id, content, resume=True):
        if not isinstance(content, str) or not content.strip() or len(content) > 100000:
            raise DomainError('请输入有效的补充指令（最多 100000 字符）')
        with self.store.transaction():
            run = self.store.run(run_id)
            if run.get('experience') != 'agent' or run['status'] not in ('queued', 'running', 'waiting'):
                raise DomainError('仅进行中的智能任务支持补充指令', 409)
            if resume and run.get('_gate_feedback_pending'):
                raise DomainError('正在应用上一条确认或补充，请稍候再发送；当前输入会保留。', 409)
            pause = run.get('interrupt', {})
            if resume and run['status'] == 'waiting' and pause.get('type') in ('clarification', 'strategy_review') and pause.get('artifact_id'):
                # Let the gate distinguish workflow control from new business
                # evidence before incrementing the instruction epoch.
                return self.engine.resume(run_id, {'answer': content.strip()})
            if resume and self.is_continue_command(content):
                evidence = self.store.evidence(run['_source_ids'])
                if not any(e['role'] != 'example' for e in evidence) or run['status'] == 'waiting':
                    raise DomainError('尚无可沿用的需求分析，请先补充要测试的业务规则。')
                decision = {'mode': 'proceed', 'epoch': run.get('_instruction_version', 0),
                            'content': content.strip(), 'instruction_recorded': True}
                self.store.update_run(run_id, _clarification_decision=decision)
                self.store.put('message', {'id': f'{run_id}:continue:{decision["epoch"]}', 'chat_id': run['chat_id'],
                    'project_id': run['project_id'], 'role': 'user', 'content': content.strip(),
                    'created_at': now(), 'metadata': {'run_id': run_id, 'workflow_decision': True}})
                self.insight(run_id, '收到继续指令：当前分析完成后继续设计，未决问题与假设将保留。', [], 'decision')
                return self.store.run(run_id)
            version = run.get('_instruction_version', 0) + 1
            text, chunks = parse_text(content.strip())
            source = self.store.add_source(run['chat_id'], '用户补充指令', 'clarification', text, chunks)
            refs = [e['id'] for e in self.store.evidence([source['id']])]
            instructions = run.get('_instructions', []) + [{'id': uid('instruction_'), 'content': text, 'refs': refs, 'version': version, 'created_at': now()}]
            run.update(_instructions=instructions, _instruction_version=version, _source_ids=run['_source_ids'] + [source['id']])
            self.store.put('message', {'id': uid('msg_'), 'chat_id': run['chat_id'], 'project_id': run['project_id'], 'role': 'user', 'content': text, 'created_at': now(), 'metadata': {'run_id': run_id, 'instruction': True}})
            chat = self.store.get('chat', run['chat_id'])
            memory = chat.get('memory', {'decisions': [], 'scope': [], 'open_questions': [], 'source_refs': []})
            memory['decisions'].append({'id': instructions[-1]['id'], 'summary': text, 'refs': refs, 'status': 'confirmed', 'run_id': run_id})
            memory['source_refs'] = list(dict.fromkeys(memory.get('source_refs', []) + refs))
            memory['updated_at'] = now()
            chat['memory'] = memory
            self.store.put('chat', chat)
            waiting = run['status'] == 'waiting'
            if waiting and resume:
                run.update(status='queued', stage='applying_instruction', _resume={'interrupt_id': run['_interrupt_id'], 'value': {'instruction': True}})
                run.pop('interrupt', None)
            self.store.save_run(run)
            self.update(run_id, pending_instructions=version - run.get('_applied_instruction_version', 0))
            self.insight(run_id, '收到补充指令，将在安全边界重新检查策略；旧范围的未发布输出将被丢弃。', refs, 'decision')
        if waiting and resume:
            self.engine.schedule(run_id)
        return self.store.run(run_id)

    async def finish(self, state):
        self.current(state)
        context = self.context(state, coverage=self.store.run(state['run_id'])['agent'].get('coverage'))
        design_goal = state['intent'] in ('generate_case', 'generate_scenario')
        if design_goal and (not context['coverage'] or context['coverage']['gaps']):
            raise IncompleteCoverage('仍有设计覆盖缺口，不能发布完成结果。')
        output_ref = state.get('output_ref') or state.get('cases_ref') or state.get('scenario_ref') or state.get('analysis_ref')
        contract.require(bool(output_ref), 'output_ref', 'accepted_output')
        context['output'] = public(self.store.get('artifact', output_ref))
        context['goal'] = state['intent']
        self.engine.stage(state['run_id'], 'summarizing')
        evidence = {e['id']: e for e in context['evidence']}
        def validate(result):
            contract.text(result.get('summary'), 'summary')
            if state['intent'] != 'learn_template':
                contract.refs(result.get('refs'), evidence, 'summary.refs')
            else:
                contract.strings(result.get('refs', []), 'summary.refs')
            return result
        result = await self.call(state, 'agent_summary', context, validate)
        with self.store.transaction():
            run = self.current(state)
            artifact = self.store.get('artifact', output_ref)
            if artifact.get('_agent_target'):
                target = artifact['_agent_target']
                review_report = artifact.get('report', {}).get('review')
                artifact = self.store.revise_artifact(target['id'], target['revision'], artifact['items'], target['reason'], run['id'], f'agent_edit_applied:{state["epoch"]}:{state["iteration"]}')
                output_ref = artifact['id']
                if review_report is not None:
                    report_artifact = self.store.artifact(run['id'], f'agent_review_report:{state["epoch"]}:{state["iteration"]}', 'review', '用例审查报告', [], review_report)
                else:
                    report_artifact = None
            else:
                report_artifact = None
            summary = result['summary']
            if design_goal:
                artifact = self.store.artifact(run['id'], f'agent_final:{state["epoch"]}:{state["iteration"]}', artifact['type'], artifact['title'], artifact['items'], {**artifact['report'], 'coverage': context['coverage']})
                output_ref = artifact['id']
                summary += f'\n设计覆盖：需求 {context["coverage"]["requirements_covered"]}/{context["coverage"]["requirements_total"]}，分支 {context["coverage"]["branches_covered"]}/{context["coverage"]["branches_total"]}。这是设计覆盖，未执行测试。'
            if context.get('deferred_questions'):
                summary += '\n按你的指示保留以下未决问题并继续设计，相关业务规则尚未确认：\n' + '\n'.join('- ' + q for q in context['deferred_questions'])
            if context.get('assumptions'):
                summary += '\n未确认的假设：\n' + '\n'.join('- ' + a for a in context['assumptions'])
            self.update(run['id'], summary=summary, plan=[{**s, 'status': 'completed'} for s in run['agent']['plan']])
            self.insight(run['id'], summary, result['refs'], 'summary')
            ids = list(dict.fromkeys(ref for ref in (state.get('analysis_ref'), state.get('scenario_ref') if state['intent'] == 'generate_case' else None, output_ref) if ref))
            if report_artifact is not None:
                ids.insert(0, report_artifact['id'])
            self.store.publish(run['id'], ids, summary, proposal=artifact.get('report', {}).get('config') if artifact['type'] == 'proposal' else None)
        return {'done': True}

    def recovery(self, run, exc):
        category = getattr(exc, 'category', 'schema' if isinstance(exc, OutputValidationError) else 'model')
        return {'category': category, 'title': '智能设计尚未完成', 'detail': f'阶段 {run["stage"]} 已停止，未发布不完整的最终用例。',
                'suggestions': ['检查设置中的认证方式、模型名称和请求头，然后重试当前阶段。'] if category in ('authentication', 'configuration') else ['检查已保存的分析、覆盖缺口和问题。', '可重试当前阶段，或取消后补充需求、缩小范围重新开始。'],
                'preserved': ['已有来源证据', '策略与已完成的 Artifact 版本', '检查点、补充指令和确认记忆'], 'retryable': getattr(exc, 'retryable', True)}
