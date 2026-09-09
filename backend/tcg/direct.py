"""Whole-document authoring; partition only when the complete request cannot fit."""
import copy
import json
import math
import re

from langgraph.types import interrupt
from .documents import parse_text
from .environment import runtime_value
from .graph import Engine
from .model import SYSTEM, TASK_INSTRUCTIONS
from .reliable import ReliableEngine
from .schemas import DomainError, validate_items


def token_estimate(value):
    # Conservative fallback for arbitrary local models; not a model tokenizer.
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    ascii_count = sum(ord(c) < 128 for c in text)
    return math.ceil(ascii_count / 3) + (len(text) - ascii_count) * 2


class DirectEngine(ReliableEngine):
    def reliable(self, run_id):
        return self.store.run(run_id).get('graph_version') in (4, 5)

    def direct(self, run_id):
        return self.store.run(run_id).get('graph_version') == 5

    def all_evidence(self, run_id):
        return [{k: e[k] for k in ('id', 'text', 'source_id', 'location', 'role')} for e in super().all_evidence(run_id)]

    def context(self, run_id, **extra):
        context = super().context(run_id, **extra)
        run = self.store.run(run_id)
        selected = run['_request'].get('selected_ids')
        if self.direct(run_id) and run['intent'] == 'modify' and selected and context.get('artifact'):
            context['artifact'] = {**context['artifact'], 'items': [row for row in context['artifact']['items'] if row['id'] in selected]}
            refs = {ref for row in context['artifact']['items'] for ref in row.get('refs', [])}
            context['evidence'] = [e for e in context['evidence'] if e['id'] in refs or e['role'] in ('change', 'clarification')]
        return context

    def limits(self):
        try:
            window = int(runtime_value(self.store.directory, 'TCG_MODEL_CONTEXT_TOKENS', '0'))
            reserve = int(runtime_value(self.store.directory, 'TCG_OUTPUT_TOKENS', '8192'))
            if window < 0 or reserve < 1024 or (window and (window < 4096 or reserve >= window - 1024)):
                raise ValueError()
        except ValueError:
            raise DomainError('模型容量配置无效：输出预留须小于上下文窗口，至少保留 1024 tokens 输入空间。') from None
        return window, reserve

    def fits(self, task, context):
        window, reserve = self.limits()
        if window == 0:
            return True
        return token_estimate(SYSTEM + TASK_INSTRUCTIONS.get(task, '')) + token_estimate(context) + 512 <= window - reserve and len(json.dumps(context, ensure_ascii=False)) <= 490000

    async def invoke_model(self, task, context, run_id=None):
        if run_id and self.direct(run_id):
            if not self.fits(task, context):
                raise DomainError('本次请求超过已配置模型容量。已保存有效草稿；请提高 TCG_MODEL_CONTEXT_TOKENS 至模型实际支持值，或缩小本轮范围。未截断业务原文。')
            return await Engine.invoke_model(self, task, context, run_id)
        return await super().invoke_model(task, context, run_id)

    async def node_route(self, state):
        run = self.store.run(state['run_id'])
        if self.direct(run['id']) and run['intent'] == 'auto' and not run.get('_artifact_snapshot'):
            self.store.update_run(run['id'], intent='generate_case')
        return await super().node_route(state)

    def after_route(self, state):
        if self.direct(state['run_id']) and state['intent'] == 'generate_case' and not state.get('done'):
            return 'single'
        return super().after_route(state)

    def partition(self, base, evidence):
        if self.fits('direct_cases', {**base, 'evidence': evidence}):
            return [evidence]
        # Prefer document/section boundaries; keep adjacent paragraph pieces together.
        sections, section = [], []
        for item in evidence:
            heading = ' · 标题' in str(item.get('location', '')) or bool(re.match(r'^(?:#{1,6}\s|\d+(?:\.\d+)*[.\s]+\S)', item['text'])) and len(item['text']) < 200
            if section and (heading or item['source_id'] != section[-1]['source_id']):
                sections.append(section)
                section = []
            section.append(item)
        if section:
            sections.append(section)
        groups, current = [], []
        # Leave room for global rules, continuation cursors and local repairs.
        def fits(values):
            return self.fits('direct_cases', {**base, 'evidence': values, 'reserved_context': 'x' * 12000})
        for section in sections:
            if fits(current + section):
                current += section
                continue
            if current:
                groups.append(current)
                current = []
            for item in section:
                if not fits(current + [item]):
                    if current:
                        groups.append(current)
                    current = []
                    if not fits([item]):
                        raise DomainError('单个段落与全局规则超过模型容量，请提高上下文配置或拆分该业务段落。')
                current.append(item)
        if current:
            groups.append(current)
        return groups

    def check_row(self, item, evidence, types):
        try:
            validate_items('cases', [item], evidence)
            if item['type'] not in types:
                raise DomainError('用例类型不在本次选择范围内')
            return None
        except DomainError as exc:
            return str(exc)

    async def accepted_page(self, run_id, key, context):
        accepted = self.store.cache_get(run_id, key + ':accepted')
        if accepted is not None:
            return accepted
        self.store.update_run(run_id, _reliable_active_prefix=key)
        result = await self.call(run_id, key + ':raw', 'direct_cases', context)
        if not isinstance(result.get('items'), list):
            raise DomainError('模型没有返回用例数组；可重试本次生成。')
        if not isinstance(result.get('has_more'), bool):
            raise DomainError('模型未明确输出是否结束；请重试生成，已有草稿保留。')
        evidence = {e['id']: e for e in context['evidence']}
        items, issues, seen = copy.deepcopy(result['items']), [], set()
        for i, item in enumerate(items):
            # IDs are owned by this page, and only become public after validation.
            if isinstance(item, dict):
                if not isinstance(item.get('id'), str) or not item['id'] or item['id'] in seen:
                    item['id'] = f'ROW-{i + 1}'
                seen.add(item['id'])
                item.setdefault('scenario_id', '')
            error = self.check_row(item, evidence, context['profile']['case_types'])
            if error:
                issues.append({'index': i, 'item': item, 'error': error})
        unresolved = []
        # Send only an invalid row and its actual source paragraphs; never resend a suite.
        for issue in issues:
            row = issue['item']
            refs = row.get('refs', []) if isinstance(row, dict) else []
            selected = [e for e in context['evidence'] if e['id'] in refs] if isinstance(refs, list) else []
            repair = {**self.small_context(run_id), 'evidence': selected or context['evidence'],
                      'invalid_row': row, 'validation_error': issue['error'], 'clarification': context.get('clarification', '')}
            repaired = None
            if self.fits('repair_case_rows', repair):
                try:
                    response = await self.call(run_id, key + f":repair:{issue['index']}", 'repair_case_rows', repair)
                    repaired = response.get('items', [None])[0] if response.get('items') else None
                    if isinstance(repaired, dict):
                        repaired['id'] = row['id'] if isinstance(row, dict) else f"ROW-{issue['index'] + 1}"
                        repaired.setdefault('scenario_id', '')
                except Exception:
                    # A repair outage must not discard the other valid rows.
                    pass
            if repaired is not None and not self.check_row(repaired, evidence, context['profile']['case_types']):
                items[issue['index']] = repaired
            else:
                unresolved.append(issue)
        bad = {issue['index'] for issue in unresolved}
        result = {**result, 'items': [row for i, row in enumerate(items) if i not in bad], 'unresolved_rows': unresolved}
        return self.store.cache_set(run_id, key + ':accepted', result)

    async def node_single(self, state):
        run_id = state['run_id']
        if not self.direct(run_id) or state['intent'] != 'generate_case':
            return await super().node_single(state)
        self.stage(run_id, 'case_generation')
        base = self.small_context(run_id, previous_items=[], cursor=None, output_token_budget=self.limits()[1])
        initial = self.store.cache_get(run_id, 'v5:initial')
        if initial is None:
            initial = self.store.cache_set(run_id, 'v5:initial', {'evidence': self.all_evidence(run_id)})
        evidence = initial['evidence']
        groups = self.partition(base, evidence)
        summaries = []
        if len(groups) > 1:
            self.progress(run_id, 'requirement_analysis', 0, len(groups), f'需求超过模型输入预算，按 {len(groups)} 组提取共同规则')
            for index, group in enumerate(groups):
                summary = await self.call(run_id, f'v5:scope:{index}', 'document_context', {**base, 'evidence': group})
                if not isinstance(summary.get('summary'), str):
                    raise DomainError('大文档全局规则提取失败，请重试。')
                summaries.append(summary)
            base['global_context'] = summaries
            base['global_context_note'] = '其他章节摘要仅用于理解范围与依赖；本组用例的具体预期必须引用本组原文。'
            if any(not self.fits('direct_cases', {**base, 'evidence': group}) for group in groups):
                raise DomainError('跨章节规则超过预留空间，请提高模型上下文配置。原始需求与提取结果已保留。')
        self.store.update_run(run_id, generation_plan={'input_groups': len(groups), 'context_tokens': self.limits()[0],
            'output_reserve': self.limits()[1], 'reason': '整份输入' if len(groups) == 1 else '完整请求超过模型输入预算', 'token_estimate': token_estimate({**base, 'evidence': evidence})})
        items, reports, unresolved, questions, signatures = [], [], [], [], {}
        for index, group in enumerate(groups):
            cursor, previous, cursors, clarification = None, [], set(), ''
            for page in range(100):
                label = '整份需求正在生成用例' if len(groups) == 1 else f'生成第 {index + 1}/{len(groups)} 个业务分组'
                if page:
                    label = '继续生成剩余用例（不重新分析需求）'
                self.progress(run_id, 'case_generation', index, len(groups), label)
                context = {**base, 'evidence': group, 'previous_items': previous, 'cursor': cursor, 'clarification': clarification}
                key = f'v5:cases:{index}:{page}'
                result = await self.accepted_page(run_id, key, context)
                blocking = result.get('questions', [])
                if not isinstance(blocking, list) or not all(isinstance(q, str) for q in blocking):
                    raise DomainError('澄清问题格式无效，请重试。')
                if blocking and not result['items'] and not clarification:
                    response = interrupt({'type': 'clarification', 'questions': blocking[:3]})
                    clarification = response['answer']
                    text, chunks = parse_text(clarification)
                    saved = self.store.cache_get(run_id, key + ':clarification')
                    if not saved:
                        source = self.store.add_source(self.store.run(run_id)['chat_id'], '用户澄清', 'clarification', text, chunks)
                        current = self.store.run(run_id)
                        self.store.update_run(run_id, _source_ids=current['_source_ids'] + [source['id']],
                            _source_roles={**current['_source_roles'], source['id']: 'clarification'})
                        saved = self.store.cache_set(run_id, key + ':clarification', {'id': source['id']})
                    group = group + [e for e in self.all_evidence(run_id) if e['source_id'] == saved['id'] and e['id'] not in {x['id'] for x in group}]
                    context = {**context, 'evidence': group, 'clarification': clarification}
                    result = await self.accepted_page(run_id, key + ':clarified', context)
                new = []
                for row in result['items']:
                    signature = json.dumps({k: row.get(k) for k in ('title', 'preconditions', 'steps', 'type')}, sort_keys=True, ensure_ascii=False)
                    if signature in signatures:
                        existing_index = signatures[signature]
                        existing = items[existing_index] if existing_index < len(items) else new[existing_index - len(items)]
                        existing['refs'] = list(dict.fromkeys(existing['refs'] + row['refs']))
                        continue
                    signatures[signature] = len(items) + len(new)
                    new.append({**row, 'id': f'TC-{len(items) + len(new) + 1:04d}'})
                items.extend(new)
                reports.append(result.get('report', {}))
                unresolved.extend(result.get('unresolved_rows', []))
                questions.extend(q for q in result.get('questions', []) if isinstance(q, str))
                if items:
                    draft = self.store.artifact(run_id, key + ':draft', 'cases', '测试用例草稿', items,
                        {'summary': '生成中的有效草稿；尚未完成所有输出。', 'questions': questions, 'unresolved_rows': unresolved})
                    self.store.update_run(run_id, draft_artifact_id=draft['id'])
                if not result['has_more']:
                    break
                next_cursor = result.get('next_cursor')
                if not new or not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
                    raise DomainError('模型续写没有产生新用例或有效游标，已保留草稿；可缩小范围后继续。')
                cursor = next_cursor
                cursors.add(cursor)
                previous.extend({'id': row['id'], 'title': row['title']} for row in new)
            else:
                raise DomainError('输出续写达到保护上限，已保留草稿。')
        if not items:
            raise DomainError('尚未生成有效业务用例。请查看执行详情，补充业务规则后重新生成。')
        artifact = self.store.artifact(run_id, 'v5:final', 'cases', '测试用例', items,
            {'summary': f'已生成 {len(items)} 条用例；未实际执行测试。', 'questions': list(dict.fromkeys(questions)),
             'segments': reports, 'unresolved_rows': unresolved,
             'limitations': (['部分条目未通过格式校验，已隔离；请核对待修复条目。'] if unresolved else []) +
                 (['跨章节覆盖请结合原文复核。'] if len(groups) > 1 else []),
             'generation': {'input_groups': len(groups), 'output_pages': len(reports)}})
        return {'output_ref': artifact['id']}
