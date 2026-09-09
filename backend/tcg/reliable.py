"""Fixed, resumable test design with bounded inputs and durable work-unit results."""
import copy
import hashlib
import json
import os
import re

from .environment import runtime_value

from .graph import Engine, batches, routing_excerpt
from .schemas import DomainError, OutputValidationError, INTENTS, apply_operations, validate_items
from .storage import public


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False))


class ContractFailure(DomainError):
    retryable = False

    def __init__(self, errors):
        self.errors = errors
        paths = ', '.join(e['path'] for e in errors[:5])
        super().__init__(f'结果校验未通过（{len(errors)}项）：{paths}；已保留完成批次，可重试当前批次', 422)


def item_errors(kind, items, evidence, scenario_ids=None):
    """Collect independent errors across all rows; never repair by changing evidence."""
    if not isinstance(items, list):
        return [{'path': 'items', 'code': 'invalid_type', 'expected': 'array'}]
    errors, seen = [], set()
    for i, item in enumerate(items):
        try:
            validate_items(kind, [item], evidence, scenario_ids)
        except OutputValidationError as exc:
            issue = {**exc.issue, 'path': exc.issue['path'].replace('items[0]', f'items[{i}]', 1)}
            errors.append(issue)
        if isinstance(item, dict):
            if encoded_size(item) > 6000:
                errors.append({'path': f'items[{i}]', 'code': 'item_budget', 'expected': '单项序列化后不超过6000字符；精简文字，保留规则与有效引用'})
            if isinstance(item.get('id'), str):
                if item['id'] in seen:
                    errors.append({'path': f'items[{i}].id', 'code': 'duplicate_id', 'expected': 'unique_id'})
                seen.add(item['id'])
            refs = item.get('refs')
            if isinstance(refs, list):
                for j, ref in enumerate(refs):
                    if isinstance(ref, str) and (ref not in evidence or evidence[ref]['role'] == 'example'):
                        path = f'items[{i}].refs[{j}]'
                        if not any(e['path'] == path for e in errors):
                            errors.append({'path': path, 'code': 'invalid_reference', 'expected': 'provided_non_example_evidence_id'})
    return errors


class ReliableEngine(Engine):
    def reliable(self, run_id):
        return self.store.run(run_id).get('graph_version') == 4

    def budget(self):
        try:
            value = int(runtime_value(self.store.directory, 'TCG_CONTEXT_CHARS', '32000'))
        except ValueError:
            raise DomainError('TCG_CONTEXT_CHARS 必须为整数') from None
        if not 12000 <= value <= 500000:
            raise DomainError('TCG_CONTEXT_CHARS 必须在12000至500000之间')
        return value

    def all_evidence(self, run_id):
        run = self.store.run(run_id)
        return [{**e, 'role': run.get('_source_roles', {}).get(e['source_id'], e['role'])}
                for e in self.store.evidence(run['_source_ids'])]

    async def node_route(self, state):
        if not self.reliable(state['run_id']):
            return await super().node_route(state)
        run_id = state['run_id']
        self.stage(run_id, 'routing')
        run = self.store.run(run_id)
        intent = run['intent']
        if intent == 'auto':
            result = await self.call(run_id, 'route', 'route', self.routing_context(run_id))
            intent = result.get('intent')
            if not isinstance(intent, str) or intent not in INTENTS:
                raise DomainError('模型返回了不支持的 Intent')
        sources = run['_source_ids']
        roles = dict(run.get('_source_roles', {}))
        if intent in ('review_case', 'query', 'modify') and run.get('_artifact_snapshot'):
            artifact = run['_artifact_snapshot']
            sources = list(dict.fromkeys(sources + run.get('_artifact_source_ids', artifact.get('_source_ids', []))))
            for sid in sources:
                roles.setdefault(sid, artifact.get('_source_roles', {}).get(sid, self.store.get('source', sid)['role']))
        self.store.update_run(run_id, intent=intent, _source_ids=sources, _source_roles=roles)
        if intent in ('review_requirement', 'generate_scenario', 'generate_case', 'review_case') and not any(e['role'] != 'example' for e in self.all_evidence(run_id)):
            output = self.answer(run_id, 'missing_source', '请上传或粘贴有效需求后重试。Example 仅供格式参考，不能作为业务需求证据。')
            return {'intent': intent, 'output_ref': output['id'], 'done': True}
        artifact = run.get('_artifact_snapshot')
        if intent == 'modify' and not artifact:
            output = self.answer(run_id, 'missing_artifact', '请先选择需要修改的 Artifact。')
            return {'intent': intent, 'output_ref': output['id'], 'done': True}
        if intent == 'review_case' and artifact:
            if artifact['type'] != 'cases':
                raise DomainError('Review Case 需要 Case Artifact 或上传的用例文件')
            return {'intent': intent, 'cases_ref': artifact['id']}
        return {'intent': intent}

    def context(self, run_id, **extra):
        if not self.reliable(run_id):
            return super().context(run_id, **extra)
        run = self.store.run(run_id)
        request = {k: run['_request'][k] for k in ('content', 'intent', 'mode')}
        if run['_request'].get('as_requirement'):
            request['content'] = '按本轮消息所保存的需求正文与所选配置完成目标。'
        history = [m for m in run.get('_conversation', []) if m.get('metadata', {}).get('run_id') != run_id]
        conversation = [{'role': m['role'], 'content': routing_excerpt(m['content'], 400)} for m in history[-6:]]
        available = max(len(history), run.get('_history_total', len(history) + 1) - 1)
        partial = available > len(conversation) or any(m['content'] != c['content'] for m, c in zip(history[-6:], conversation))
        context = {'request': request, 'profile': run['_profile'],
                   'confirmed_memory': [{k: m[k] for k in ('kind', 'content')} for m in run.get('_memory', [])],
                   'conversation': conversation,
                   'conversation_context': {'partial': partial, 'available_messages': available,
                                            'note': '历史为有限摘录；完整需求以本次提供的证据与确认记忆为准。'},
                   'evidence': self.all_evidence(run_id),
                   'selected_ids': run['_request'].get('selected_ids'),
                   'artifact': public(run['_artifact_snapshot']) if run.get('_artifact_snapshot') else None}
        context.update(extra)
        return context

    def small_context(self, run_id, **extra):
        context = self.context(run_id, evidence=[], artifact=None, **extra)
        context['depth_guidance'] = {
            'quick': '为选定范围设计核心路径和最高风险的代表性用例，避免低价值组合；列出未展开组合。',
            'standard': '覆盖主要业务规则、异常与边界；用例步骤清楚、预期可观察。',
            'deep': '增加状态迁移、权限、条件组合和跨模块风险；只依据已提供规则。',
        }[context['profile']['case_level']]
        return context

    def with_evidence(self, run_id, fields, include_changes=False):
        refs = {r for values in fields.values() if isinstance(values, list)
                for item in values if isinstance(item, dict) for r in item.get('refs', [])}
        # Changes are analyzed exhaustively as their own work items. Reattaching
        # every change document would make a single downstream item unbounded.
        evidence = [e for e in self.all_evidence(run_id) if e['id'] in refs]
        context = self.small_context(run_id, **fields)
        context['evidence'] = evidence
        return context

    def groups(self, run_id, items, field, context_builder=None):
        """Partition by complete serialized input, reserving space for the output repair."""
        groups, current = [], []
        ceiling = self.budget() - 8000
        context_builder = context_builder or (lambda values: self.with_evidence(run_id, {field: values}))
        for item in items:
            proposed = current + [item]
            if encoded_size(context_builder(proposed)) > ceiling or encoded_size(proposed) > 6000:
                if current:
                    groups.append(current)
                    current = []
                if encoded_size(context_builder([item])) > ceiling:
                    raise ContractFailure([{'path': field, 'code': 'context_budget', 'expected': '单个工作项与确认规则须能装入上下文；可提高TCG_CONTEXT_CHARS'}])
            current.append(item)
        if current:
            groups.append(current)
        return groups

    def progress(self, run_id, phase, completed, total, label):
        self.store.assert_running(run_id)
        self.store.update_run(run_id, progress={'phase': phase, 'completed': completed, 'total': total, 'label': label})

    async def invoke_model(self, task, context, run_id=None):
        if run_id and self.reliable(run_id):
            # Include actual system/task prompt size when using the built-in gateway.
            from .model import SYSTEM, TASK_INSTRUCTIONS
            size = encoded_size(context) + len(SYSTEM) + len(TASK_INSTRUCTIONS.get(task, '')) + 200
            if size > self.budget():
                raise ContractFailure([{'path': 'context', 'code': 'context_budget',
                                        'expected': f'输入约{size}字符，预算{self.budget()}；请缩小单批或提高预算，原文未截断'}])
        return await super().invoke_model(task, context, run_id)

    async def prepare_result(self, run_id, task, context, result):
        return result

    async def validated(self, run_id, prefix, task, context, validator):
        self.store.update_run(run_id, _reliable_active_prefix=prefix)
        digest = hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        previous_digest = self.store.cache_get(run_id, prefix + ':context')
        accepted = self.store.cache_get(run_id, prefix + ':accepted')
        if accepted is not None and previous_digest in (None, digest) and not validator(accepted):
            return accepted
        if accepted is not None or previous_digest not in (None, digest):
            for row in self.store.db.execute('SELECT key FROM cache WHERE run_id=?', (run_id,)).fetchall():
                if row['key'].startswith(prefix + ':'):
                    self.store.cache_delete(run_id, row['key'])
        self.store.cache_set(run_id, prefix + ':context', digest)
        recovery_key='recovery_hint:'+prefix
        hint=self.store.cache_get(run_id,recovery_key)
        request_context={**context, **({'validation_repair':hint} if hint else {})}
        result = await self.call(run_id, prefix + ':raw', task, request_context)
        result = await self.prepare_result(run_id, task, context, result)
        self.store.update_run(run_id,_reliable_active_prefix=prefix)
        original = copy.deepcopy(result)
        original_errors = validator(original)
        valid_original = {}
        if isinstance(original.get('items'), list):
            kind = {'analyze_requirement': 'analysis', 'generate_scenarios': 'scenarios', 'generate_cases': 'cases'}.get(task)
            if kind:
                evidence = {e['id']: e for e in context['evidence']}
                for index, item in enumerate(original['items']):
                    affected = any(e['path'] == f'items[{index}]' or e['path'].startswith(f'items[{index}].') for e in original_errors)
                    if not affected and not item_errors(kind, [item], evidence):
                        valid_original[item['id']] = copy.deepcopy(item)
        for attempt in range(3):
            errors = validator(result)
            if attempt and isinstance(result.get('items'), list) and isinstance(original.get('items'), list):
                # Preserve accepted rows locally; the model only repairs invalid rows/report.
                result = copy.deepcopy(result)
                result_by_id = {i.get('id'): i for i in result['items'] if isinstance(i, dict)}
                for row in original['items']:
                    if isinstance(row, dict) and row.get('id') not in result_by_id:
                        result['items'].append(copy.deepcopy(row))
                result['items'] = [copy.deepcopy(valid_original.get(row.get('id'), row)) if isinstance(row,dict) else row for row in result['items']]
                errors = validator(result)
            if not errors:
                return self.store.cache_set(run_id, prefix + ':accepted', result)
            self.trace('batch.validation_failed', run_id, errors=errors, repair_attempt=attempt)
            self.store.cache_set(run_id,recovery_key,{'errors':errors,'previous_response':result,'instruction':'上次输出未通过。换一种表达修复这些具体错误；只输出符合任务契约的 JSON，不编造来源。'})
            if attempt == 2:
                raise ContractFailure(errors)
            repair = {**context, 'validation_repair': {'errors': errors, 'previous_response': result,
                      'instruction': '一次修复列表中的全部错误。返回完整结果，保留合法条目和稳定ID；不可删除条目或编造证据来通过校验。'}}
            result = await self.call(run_id, prefix + f':repair:{attempt}', task, repair)
            result = await self.prepare_result(run_id, task, context, result)
            self.store.update_run(run_id,_reliable_active_prefix=prefix)
        raise AssertionError('unreachable')

    async def node_analysis(self, state):
        run_id = state['run_id']
        if not self.reliable(run_id):
            return await super().node_analysis(state)
        self.stage(run_id, 'requirement_analysis')
        evidence = [e for e in self.all_evidence(run_id) if e['role'] != 'example']
        groups = batches(evidence, budget=4500)
        merged, reports = [], []
        for index, group in enumerate(groups):
            self.progress(run_id, 'requirement_analysis', index, len(groups), f'分析需求批次 {index + 1}/{len(groups)}')
            context = self.small_context(run_id, batch_index=index, batch_count=len(groups))
            context['evidence'] = group
            context['output_contract'] = ('对每个证据块提取规则，items使用稳定ID和精确refs。无规则的证据块列入report.excluded_evidence:[{id,reason}]。'
                                          '每个输入证据ID必须出现在items.refs或有理由的excluded_evidence中。report.questions和assumptions为字符串数组。')
            evidence_map = {e['id']: e for e in group}
            def validate(result):
                errors = item_errors('analysis', result.get('items'), evidence_map)
                report = result.get('report', {})
                if not isinstance(report, dict):
                    return errors + [{'path': 'report', 'code': 'type', 'expected': 'object'}]
                for name in ('questions', 'assumptions'):
                    if not isinstance(report.get(name, []), list) or not all(isinstance(v, str) for v in report.get(name, [])):
                        errors.append({'path': 'report.' + name, 'code': 'type', 'expected': 'string_array'})
                covered = {r for i in result.get('items', []) if isinstance(i, dict) and isinstance(i.get('refs'), list) for r in i['refs'] if isinstance(r, str)} if isinstance(result.get('items'), list) else set()
                exclusions = report.get('excluded_evidence', [])
                if not isinstance(exclusions, list):
                    errors.append({'path': 'report.excluded_evidence', 'code': 'type', 'expected': 'array'})
                    exclusions = []
                for e in exclusions:
                    if isinstance(e, dict) and isinstance(e.get('id'), str) and e['id'] in evidence_map and isinstance(e.get('reason'), str) and e['reason'].strip():
                        covered.add(e['id'])
                    else:
                        errors.append({'path': 'report.excluded_evidence', 'code': 'invalid_exclusion', 'expected': 'provided_id_and_reason'})
                if missing := evidence_map.keys() - covered:
                    errors.append({'path': 'items.refs', 'code': 'unprocessed_evidence', 'expected': sorted(missing)})
                return errors
            result = await self.validated(run_id, f'v4:analysis:{index}', 'analyze_requirement', context, validate)
            merged.extend({**i, 'id': f'R{index + 1}-{i["id"]}'} for i in result['items'])
            reports.append({**result.get('report', {}), 'changes': {kind: sum(op['op'] == kind for op in result['operations']) for kind in ('add','update','delete')}})
            self.progress(run_id, 'requirement_analysis', index + 1, len(groups), '需求批次已保存')
        report = {'questions': list(dict.fromkeys(q for r in reports for q in r.get('questions', []))),
                  'assumptions': list(dict.fromkeys(q for r in reports for q in r.get('assumptions', []))),
                  'requirement_map': {'segments': [r.get('requirement_map', {}) for r in reports]},
                  'diagrams': [d for r in reports for d in r.get('diagrams', [])],
                  'source_coverage': {'processed_chunks': len(evidence), 'total_chunks': len(evidence)},
                  'limitations': ['按证据引用分批设计；跨批次冲突与隐含依赖不保证自动合并，请在场景确认时核对。'],
                  'excluded_evidence': [e for r in reports for e in r.get('excluded_evidence', [])]}
        artifact = self.store.artifact(run_id, 'v4:analysis_artifact', 'analysis', '需求分析', merged, report)
        return {'analysis_ref': artifact['id'], 'output_ref': artifact['id']}

    async def node_clarify(self, state):
        if not self.reliable(state['run_id']):
            return await super().node_clarify(state)
        from langgraph.types import interrupt
        from .documents import parse_text
        run_id = state['run_id']
        run = self.store.run(run_id)
        analysis = self.store.get('artifact', state['analysis_ref'])
        if run['mode'] != 'hitp':
            return {}
        round_index = 0
        while analysis.get('report', {}).get('questions'):
            answer = interrupt({'type': 'clarification', 'questions': analysis['report']['questions']})
            if not isinstance(answer, dict) or not isinstance(answer.get('answer'), str) or not answer['answer'].strip():
                raise DomainError('请填写澄清答案后继续')
            source_key = 'v4:clarification' if not round_index else f'v4:clarification:{round_index}'
            saved = self.store.cache_get(run_id, source_key)
            if not saved:
                text, chunks = parse_text(answer['answer'])
                with self.store.transaction():
                    source = self.store.add_source(run['chat_id'], '用户澄清', 'clarification', text, chunks)
                    current = self.store.run(run_id)
                    current['_source_ids'].append(source['id'])
                    current['_source_roles'][source['id']] = 'clarification'
                    self.store.save_run(current)
                    saved = self.store.cache_set(run_id, source_key, {'id': source['id']})
            answer_groups = batches([e for e in self.all_evidence(run_id) if e['source_id'] == saved['id']], budget=4500)
            reports = []
            # Every answer chunk is reconciled against every requirement group;
            # only cited clarification evidence survives into downstream work.
            for answer_index, answer_group in enumerate(answer_groups):
                text = ''.join(e['text'] for e in answer_group)
                def build_context(values):
                    context = self.with_evidence(run_id, {'analysis': values})
                    known = {e['id'] for e in context['evidence']}
                    context['evidence'].extend(e for e in answer_group if e['id'] not in known)
                    context['clarification'] = text
                    context['pending_questions'] = analysis.get('report', {}).get('questions', [])
                    context['output_contract'] = ('返回完整已修订需求items和report。保留每个输入analysis.id和原refs；'
                        '修订规则须说明澄清依据，新增引用只能使用提供的证据。不得遗漏无关规则。'
                        'report.questions必须为尚未解决的问题字符串数组，已全部解决才返回[]。')
                    return context
                groups = self.groups(run_id, analysis['items'], 'analysis', build_context)
                items, reports = [], []
                for index, group in enumerate(groups):
                    context = build_context(group)
                    evidence = {e['id']: e for e in context['evidence']}
                    originals = {i['id']: i for i in group}
                    def validate(result):
                        errors = item_errors('analysis', result.get('items'), evidence)
                        revised = {i['id']: i for i in result.get('items', [])
                                   if isinstance(i, dict) and isinstance(i.get('id'), str)} if isinstance(result.get('items'), list) else {}
                        if missing := originals.keys() - revised.keys():
                            errors.append({'path': 'items', 'code': 'lost_requirements', 'expected': sorted(missing)})
                        for key, original in originals.items():
                            refs = revised.get(key, {}).get('refs', [])
                            if not isinstance(refs, list) or not set(original.get('refs', [])) <= {r for r in refs if isinstance(r, str)}:
                                errors.append({'path': 'items', 'code': 'lost_grounding', 'expected': {'id': key, 'refs': original.get('refs', [])}})
                        report = result.get('report')
                        if not isinstance(report, dict) or not isinstance(report.get('questions'), list) or not all(isinstance(q, str) for q in report['questions']):
                            errors.append({'path': 'report.questions', 'code': 'type', 'expected': 'remaining_question_string_array'})
                        return errors
                    prefix = f'v4:clarify:{round_index}:{answer_index}:{index}'
                    result = await self.validated(run_id, prefix, 'analyze_requirement', context, validate)
                    items.extend(result['items'])
                    reports.append(result['report'])
                errors = item_errors('analysis', items, {e['id']: e for e in self.all_evidence(run_id)})
                if errors:
                    raise ContractFailure(errors)
                questions = list(dict.fromkeys(q for report in reports for q in report['questions']))
                analysis = self.store.artifact(run_id, f'v4:clarified_artifact:{round_index}:{answer_index}',
                    'analysis', '已澄清需求', items, {**analysis.get('report', {}), 'questions': questions,
                    'clarification': answer['answer']})
            round_index += 1
        return {'analysis_ref': analysis['id'], 'output_ref': analysis['id']}

    async def generate_groups(self, state, phase, task, kind, input_field, inputs):
        run_id = state['run_id']
        self.stage(run_id, phase)
        groups = self.groups(run_id, inputs, input_field)
        if not groups:
            raise ContractFailure([{'path': input_field, 'code': 'no_grounded_input', 'expected': '至少一项有依据的输入'}])
        merged = []
        for index, group in enumerate(groups):
            self.progress(run_id, phase, index, len(groups), f'处理批次 {index + 1}/{len(groups)}')
            context = self.with_evidence(run_id, {input_field: group})
            evidence = {e['id']: e for e in context['evidence']}
            input_ids = {i['id'] for i in group}
            previous, cursor, cursors = [], None, []
            try:
                max_pages = int(runtime_value(self.store.directory, 'TCG_MAX_PAGES_PER_BATCH', '30'))
                if not 1 <= max_pages <= 1000:
                    raise ValueError()
            except ValueError:
                raise DomainError('TCG_MAX_PAGES_PER_BATCH 必须为整数') from None
            for page in range(max_pages):
                page_context = {**context, 'cursor': cursor,
                    'previous_items': [{'id': i['id'], 'title': i['title']} for i in previous],
                    'output_contract': ('返回items,has_more,next_cursor。仅返回本页新增条目，ID不可重复。'
                        '场景必须有requirement_ids且逐项覆盖输入analysis.id；用例scenario_id必须属于输入scenarios。'
                        '每个输入必须被覆盖；type只能取profile.case_types。按depth_guidance控制组合复杂度。')}
                def validate(result):
                    items = result.get('items')
                    errors = item_errors(kind, items, evidence, input_ids if kind == 'cases' else None)
                    if not isinstance(items, list):
                        return errors
                    old_ids = {i['id'] for i in previous}
                    for j, item in enumerate(items):
                        if not isinstance(item, dict):
                            continue
                        if isinstance(item.get('id'), str) and item['id'] in old_ids:
                            errors.append({'path': f'items[{j}].id', 'code': 'duplicate_page_id', 'expected': 'new_id'})
                        if kind == 'cases' and item.get('type') not in context['profile']['case_types']:
                            errors.append({'path': f'items[{j}].type', 'code': 'unselected_type', 'expected': context['profile']['case_types']})
                        if kind == 'scenarios':
                            refs = item.get('requirement_ids')
                            if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r in input_ids for r in refs):
                                errors.append({'path': f'items[{j}].requirement_ids', 'code': 'requirement_coverage', 'expected': sorted(input_ids)})
                    if not isinstance(result.get('has_more'), bool):
                        errors.append({'path': 'has_more', 'code': 'type', 'expected': 'boolean'})
                    elif result['has_more']:
                        next_cursor = result.get('next_cursor')
                        if not items or not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor or next_cursor in cursors:
                            errors.append({'path': 'next_cursor', 'code': 'no_progress', 'expected': 'new_cursor_and_new_items'})
                    else:
                        covered = set()
                        for item in previous + items:
                            if isinstance(item, dict):
                                if kind == 'cases' and isinstance(item.get('scenario_id'), str):
                                    covered.add(item['scenario_id'])
                                elif kind == 'scenarios' and isinstance(item.get('requirement_ids'), list):
                                    covered.update(r for r in item['requirement_ids'] if isinstance(r, str))
                        if missing := input_ids - covered:
                            errors.append({'path': 'items', 'code': 'missing_coverage', 'expected': sorted(missing)})
                    return errors
                prefix = f'v4:{kind}:{index}:page:{page}'
                result = await self.validated(run_id, prefix, task, page_context, validate)
                previous.extend(result['items'])
                if not result['has_more']:
                    break
                cursors.append(cursor)
                cursor = result['next_cursor']
            else:
                raise ContractFailure([{'path': 'pagination', 'code': 'page_budget', 'expected': '单批分页次数超限；请调整范围后新建任务'}])
            merged.extend({**item, 'id': f'{kind[0].upper()}{index + 1}-{item["id"]}'} for item in previous)
            self.progress(run_id, phase, index + 1, len(groups), '批次已保存')
        return merged

    async def node_scenarios(self, state):
        if not self.reliable(state['run_id']):
            return await super().node_scenarios(state)
        analysis = self.store.get('artifact', state['analysis_ref'])
        items = await self.generate_groups(state, 'scenario_generation', 'generate_scenarios', 'scenarios', 'analysis', analysis['items'])
        artifact = self.store.artifact(state['run_id'], 'v4:scenarios_artifact', 'scenarios', '测试场景', items,
                                      {'requirements_total': len(analysis['items']), 'design_coverage': '每项输入需求均有关联场景；未执行测试'})
        return {'scenario_ref': artifact['id'], 'output_ref': artifact['id']}

    async def node_cases(self, state):
        if not self.reliable(state['run_id']) or state['intent'] == 'review_case':
            return await super().node_cases(state)
        scenarios = self.store.get('artifact', state['scenario_ref'])['items']
        items = await self.generate_groups(state, 'case_generation', 'generate_cases', 'cases', 'scenarios', scenarios)
        artifact = self.store.artifact(state['run_id'], 'v4:cases_artifact', 'cases', '测试用例', items)
        return {'cases_ref': artifact['id']}

    async def node_review(self, state):
        run_id = state['run_id']
        if not self.reliable(run_id):
            return await super().node_review(state)
        self.stage(run_id, 'case_review')
        artifact = self.store.get('artifact', state['cases_ref'])
        if self.store.cache_get(run_id, 'v4:review_applied'):
            return {'output_ref': artifact['id']}
        snapshot = self.store.run(run_id).get('_artifact_snapshot') if state['intent'] == 'review_case' else None
        snapshot = snapshot or artifact
        scenario_items = self.store.get('artifact', state['scenario_ref'])['items'] if state.get('scenario_ref') else []
        reserved_ids = {i['id'] for i in snapshot['items']}
        def review_context(chosen):
            ids = {i['scenario_id'] for i in chosen}
            return self.with_evidence(run_id, {'cases': chosen, 'scenarios': [s for s in scenario_items if s['id'] in ids]})
        groups = self.groups(run_id, snapshot['items'], 'cases', review_context)
        final, reports = [], []
        for index, group in enumerate(groups):
            self.progress(run_id, 'case_review', index, len(groups), f'审核批次 {index + 1}/{len(groups)}')
            ids = {i['scenario_id'] for i in group}
            scenarios = [s for s in scenario_items if s['id'] in ids]
            context = self.with_evidence(run_id, {'cases': group, 'scenarios': scenarios})
            evidence = {e['id']: e for e in context['evidence']}
            context['output_contract'] = '一次逻辑审核的当前批次。只做ADD/UPDATE/DELETE局部修改；保留每个场景至少一个有效用例，不扩大类型范围。'
            def operations_for(result):
                operations = result.get('operations')
                # Check the model's local contract before assigning server IDs.
                apply_operations(group, operations, self.store.run(run_id)['_request'].get('selected_ids'))
                renamed, normalized = {}, []
                for op_index, operation in enumerate(operations):
                    operation = copy.deepcopy(operation)
                    if operation['op'] == 'add':
                        original_id = operation['item']['id']
                        digest = hashlib.sha256(f'{run_id}:{index}:{op_index}:{original_id}'.encode()).hexdigest()
                        new_id = 'review-' + digest
                        while new_id in reserved_ids:
                            new_id += '-a'
                        renamed[original_id] = new_id
                        operation['item']['id'] = new_id
                    elif operation['id'] in renamed:
                        operation['id'] = renamed[operation['id']]
                        if isinstance(operation.get('item'), dict) and 'id' in operation['item']:
                            operation['item']['id'] = operation['id']
                    normalized.append(operation)
                return normalized
            def validate(result):
                try:
                    revised = apply_operations(group, operations_for(result), self.store.run(run_id)['_request'].get('selected_ids'))
                except OutputValidationError as exc:
                    return [exc.issue]
                errors = item_errors('cases', revised, evidence, ids if scenarios else None)
                if ids - {i.get('scenario_id') for i in revised if isinstance(i.get('scenario_id'), str)}:
                    errors.append({'path': 'operations', 'code': 'lost_scenario_coverage', 'expected': sorted(ids)})
                allowed = self.store.run(run_id)['_profile']['case_types']
                if state['intent'] != 'review_case' and any(i.get('type') not in allowed for i in revised):
                    errors.append({'path': 'operations', 'code': 'unselected_type', 'expected': allowed})
                if not isinstance(result.get('report', {}), dict):
                    errors.append({'path': 'report', 'code': 'type', 'expected': 'object'})
                return errors
            result = await self.validated(run_id, f'v4:review:{index}', 'review_cases', context, validate)
            revised = apply_operations(group, operations_for(result), self.store.run(run_id)['_request'].get('selected_ids'))
            final.extend(revised)
            reports.append({**result.get('report', {}), 'changes': {kind: sum(op['op'] == kind for op in result['operations']) for kind in ('add','update','delete')}})
            self.progress(run_id, 'case_review', index + 1, len(groups), '审核批次已保存')
        evidence = {e['id']: e for e in self.all_evidence(run_id)}
        errors = item_errors('cases', final, evidence, {s['id'] for s in scenario_items} if scenario_items else None)
        if errors:
            raise ContractFailure(errors)
        with self.store.transaction():
            updated = self.store.revise_artifact(artifact['id'], snapshot['revision'], final, 'ai_review', run_id, 'v4:review_applied')
            self.store.cache_set(run_id, 'v4:review_reports', reports)
        return {'output_ref': updated['id']}

    async def node_finish(self, state):
        result = await super().node_finish(state)
        if self.reliable(state['run_id']):
            run = self.store.run(state['run_id'])
            progress = run.get('progress', {})
            self.store.update_run(run['id'], progress={**progress, 'phase': 'completed', 'label': '已完成并保存',
                                                       'completed': progress.get('total', 0)})
        return result

    def retry(self, run_id):
        if self.reliable(run_id):
            with self.store.transaction():
                run = self.store.run(run_id)
                if run['status'] != 'failed':
                    raise DomainError('仅失败任务可重试', 409)
                prefix = run.get('_reliable_active_prefix')
                if prefix:
                    rows = self.store.db.execute('SELECT key FROM cache WHERE run_id=?', (run_id,)).fetchall()
                    for row in rows:
                        if row['key'].startswith(prefix + ':') and not row['key'].endswith(':accepted'):
                            self.store.cache_delete(run_id, row['key'])
        return super().retry(run_id)
