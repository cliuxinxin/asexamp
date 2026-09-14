"""Evidence-grounded data services shared by native tools and the pipeline.

No workflow interrupts, conversational routing, or edit leases live here. Each
write validates typed rows and commits a short optimistic database transaction.
"""
import copy
import re
from contextlib import nullcontext

from . import dependencies as deps
from .analysis_diagrams import complete_analysis_diagrams
from .case_fields import (MANUAL_FIELDS, column_signature, filled, materialize_fields,
                          protect_non_ai_fields, template_check, template_columns)
from .clarification import pending_questions, question_key, question_options, submitted_answers
from .generation_repair import GenerationRepairExhausted, repair_submission
from .native_schemas import ANSWER_SCHEMA, ESTIMATE_SCHEMA, completion_schema, rows_schema
from .reference_repair import repair_reference_fields
from .prompt_loader import load_prompt
from .schemas import DomainError, independent_item, validate_items
from .server_capacity import ContextCapacityError




def _writes():
    from . import operations
    return getattr(operations, 'native_writes', nullcontext)()


def _rows_digest(rows):
    return {row['id']: deps.digest(row) for row in rows}


def _global_rules(artifact):
    report = artifact.get('report', {})
    return {key: copy.deepcopy(report.get(key) or []) for key in ('in_scope', 'out_of_scope', 'assumptions')}


def artifact_profile(artifact, default=None):
    """Use the saved table column contract without changing its historical Profile."""
    profile = copy.deepcopy(default if default is not None else artifact.get('_profile', {}))
    columns = artifact.get('report', {}).get('table_columns')
    if artifact.get('type') == 'cases' and isinstance(columns, list):
        profile['excel_columns'] = copy.deepcopy(columns)
    return profile


def _case_fields(rows, profile, original=()):
    profile = copy.deepcopy(profile)
    columns = profile.setdefault('excel_columns', [])
    known = {column['field'] for column in columns}
    for row in list(rows) + list(original):
        for field in row:
            if field not in known and re.sub(r'[\s_-]', '', field).lower() in MANUAL_FIELDS:
                columns.append({'field': field, 'header': field, 'value_source': 'manual'})
                known.add(field)
    return protect_non_ai_fields(rows, profile, original)


def _report(value):
    report = copy.deepcopy(value.get('report') or {})
    # Linkage and process metadata are exclusively server-owned.
    for key in list(report):
        if key.startswith('_') or key in ('lineage', 'template_check', 'review_reports',
                                         'source_coverage', 'template_completion', 'clarification_followups',
                                         'source_provenance', 'shared_facts_used'):
            report.pop(key)
    return report


class NativeBusiness:
    def __init__(self, store, gateway, settings=None):
        self.store, self.gateway, self.settings = store, gateway, settings

    def _run(self, run):
        return self.store.run(run if isinstance(run, str) else run['id'])

    def _artifact(self, artifact):
        return self.store.get('artifact', artifact if isinstance(artifact, str) else artifact['id'])

    def _existing(self, run, kind):
        cached = self.store.cache_get(run['id'], 'native:artifact:' + kind)
        if cached:
            return self._artifact(cached['id'])
        matches = [self._artifact(aid) for aid in run.get('artifact_ids', [])]
        matches = [a for a in matches if a['type'] == kind]
        return matches[-1] if matches else None

    def _evidence(self, holder, source_ids=None, source_roles=None):
        from .conversation_facts import sources_allowed, ensure_artifact_allowed
        if holder.get('type') in ('analysis', 'scenarios', 'cases'):
            ensure_artifact_allowed(self.store, holder)
        sources = list(dict.fromkeys(list(holder.get('_source_ids') or []) + list(source_ids or [])))
        sources = sources_allowed(self.store, holder['chat_id'], sources, strict=True)
        roles = {**holder.get('_source_roles', {}), **(source_roles or {})}
        for sid in sources:
            source = self.store.get('source', sid)
            if source['project_id'] != holder['project_id'] or not source.get('_active', True):
                raise DomainError('资料不属于当前项目或已经停用')
        return sources, roles, self.store.evidence(sources, roles)

    def _context(self, holder, evidence, **fields):
        profile = copy.deepcopy(holder.get('_profile', {}))
        return {'profile': profile, 'template_contract': template_columns(profile),
            'evidence': [copy.deepcopy(e) for e in evidence if e.get('role') != 'example'],
            'format_references': [copy.deepcopy(e) for e in evidence if e.get('role') == 'example'],
            'instruction': holder.get('_request', {}).get('content', ''),
            'legacy_unlinked_cases': copy.deepcopy(holder.get('report', {}).get('_legacy_unlinked_cases', {})), **fields}

    def _manifest(self, sources, parents=()):
        return deps.manifest(self.store, source_ids=sources,
            artifact_ids=[{'id': a['id'], 'revision': a['revision']} for a in parents])

    async def _call(self, task, context, schema, instruction):
        return await self.gateway.generate_native(task, context, schema, load_prompt('business.policy') + instruction)

    async def _groups(self, task, rows, build, schema, instruction, run=None):
        """Send the full input first; split only an explicit server rejection.

        Durable successful leaves avoid re-running them after a later failure.
        The complete result remains unpublished until all leaves validate.
        """
        context = build(rows)
        if run:
            from .conversation_facts import record_context_usage
            record_context_usage(self.store, run, context.get('evidence', []))
        key = 'native:model:' + deps.digest({'task': task, 'context': context, 'schema': schema,
                                            'instruction': instruction,
                                            'policy': load_prompt('business.policy'),
                                            'native_system': load_prompt('native.system')})
        if run:
            cached = self.store.cache_get(run['id'], key)
            if cached is not None:
                return [cached]
        candidate_key = key + ':repair_history'
        previous_state = self.store.cache_get(run['id'], candidate_key) if run else None
        retry_round = self._run(run).get('_content_retry_round', 0) if run else 0
        def save_history(value):
            self.store.cache_set(run['id'], candidate_key, value)
            # Each explicit retry round keeps its complete original responses.
            self.store.cache_set(run['id'], candidate_key + ':' + str(retry_round), value)
        def save_progress(value):
            self.store.update_run(run['id'], repair_progress=value)
        diagnostics = getattr(self.gateway, 'diagnostics', None)
        diagnostic_scope = diagnostics.bind(run_id=run['id'], chat_id=run['chat_id'],
            project_id=run['project_id'], stage=task) if diagnostics and run else nullcontext()
        try:
            with diagnostic_scope:
                result = await repair_submission(self._call, self._validate_submission,
                    task, context, schema, instruction, diagnostics=diagnostics,
                    save_history=save_history if run else None, candidate_key=candidate_key,
                    previous_state=previous_state, retry_round=retry_round,
                    save_progress=save_progress if run else None)
        except DomainError as exc:
            output_capacity = getattr(exc, 'category', None) == 'output_capacity'
            if not isinstance(exc, ContextCapacityError) and not output_capacity:
                raise
            if len(rows) < 2:
                if output_capacity:
                    error = DomainError('单个完整业务条目的生成结果仍超过模型输出额度；请提高输出额度或拆分该条目，已有成果已保留。')
                    error.category = 'output_capacity'
                    raise error from None
                raise ContextCapacityError(message='模型服务器拒绝了单个完整业务条目；请拆分该条需求或使用容量更大的模型，已保存成果保持不变。') from None
            middle = len(rows) // 2
            left = await self._groups(task, rows[:middle], build, schema, instruction, run)
            try:
                right = await self._groups(task, rows[middle:], build, schema, instruction, run)
            except GenerationRepairExhausted as error:
                error.completed_batches = copy.deepcopy(left) + error.completed_batches
                raise
            return left + right
        if run:
            self.store.cache_set(run['id'], key, result)
        return [result]

    def _validate_submission(self, task, context, result):
        kind = {'understand_requirements': 'analysis', 'generate_scenarios': 'scenarios',
                'generate_cases': 'cases', 'review_cases': 'cases'}.get(task)
        if task == 'revise_artifact':
            kind = context['artifact_type']
        if task == 'revise_artifact' and kind in ('scenarios', 'cases'):
            from .dialogue_lineage import normalize_independent_rows
            result['items'] = normalize_independent_rows(kind, result['items'], context.get('previous_items', []),
                context.get('independent_reason', '用户明确设置为 N/A'),
                authorized_ids=context.get('independent_item_ids', []),
                allow_new=context.get('independent_addition', False),
                source_id=context.get('independent_source_id'), force=context.get('independent_edit', False))
        direct_cases = task == 'generate_cases' and context.get('generation_mode') == 'direct_requirements'
        if task in ('generate_scenarios', 'generate_cases'):
            from .dialogue_lineage import normalize_independent_rows
            result['items'] = normalize_independent_rows(kind, result['items'], context.get('previous_items', []),
                '用户明确跳过场景，直接根据需求生成用例' if direct_cases else '已有独立条目',
                authorized_ids=[], allow_new=direct_cases)
            if direct_cases:
                for row in result['items']:
                    if row.get('scenario_id') != '':
                        raise DomainError('直接生成用例时场景必须为 N/A，不得编造场景编号')
                    if not row.get('requirement_ids'):
                        raise DomainError('直接生成的用例必须关联已提供的需求编号')
                    row['_independent_origin'] = {'reason': '用户明确跳过场景，直接根据需求生成用例',
                                                  'mode': 'direct_requirements'}
        if task == 'review_cases':
            from .dialogue_lineage import normalize_independent_rows
            direct_review = context.get('generation_mode') == 'direct_requirements'
            result['items'] = normalize_independent_rows('cases', result['items'], context['cases'],
                '直接根据需求补充的评审用例' if direct_review else '已存在的独立用例',
                authorized_ids=[], allow_new=direct_review)
            if direct_review:
                existing_ids = {r['id'] for r in context['cases']}
                for row in result['items']:
                    if row['id'] not in existing_ids:
                        if row.get('scenario_id') != '' or not row.get('requirement_ids'):
                            raise DomainError('直接需求用例的评审新增项必须关联真实需求，并保持场景 N/A')
                        row['_independent_origin'] = {'reason': '直接根据需求补充的评审用例',
                                                      'mode': 'direct_requirements'}
        if kind:
            # Invalid business rows must not become a reusable successful leaf.
            parents = [{'type': key, 'items': context[key]} for key in ('analysis', 'scenarios') if key in context]
            for parent in parents:
                if parent['type'] == 'scenarios':
                    parent['_legacy_unlinked_cases'] = context.get('legacy_unlinked_cases', {})
            self._validate(kind, result['items'], context['evidence'], parents, context.get('profile'))
        if task in ('generate_scenarios', 'generate_cases'):
            parent_key = 'analysis' if task == 'generate_scenarios' or direct_cases else 'scenarios'
            expected = {row['id'] for row in context[parent_key]}
            covered = {rid for row in result['items'] for rid in (
                row['requirement_ids'] if task == 'generate_scenarios' or direct_cases else [row['scenario_id']])}
            if not expected <= covered:
                error = DomainError('模型未覆盖本批全部输入：' + '、'.join(sorted(expected - covered)))
                error.category = 'coverage'
                error.details = {'parent_type': parent_key,
                    'input_ids': sorted(expected), 'covered_ids': sorted(expected & covered),
                    'missing_input_ids': sorted(expected - covered)}
                error.item_ids = sorted(expected - covered)
                raise error
            if not {row['id'] for row in context.get('previous_items', [])} <= {row['id'] for row in result['items']}:
                raise DomainError('更新遗漏了仍然有效的已有条目；未保存本次结果')
        if task == 'review_cases':
            evidence_ids = {e['id'] for e in context['evidence'] if e.get('role') != 'example'}
            excluded = set()
            for exclusion in result.get('report', {}).get('excluded_scenarios', []):
                if not exclusion.get('reason', '').strip() or not exclusion.get('refs') or not set(exclusion['refs']) <= evidence_ids:
                    raise DomainError('评审排除缺少理由或有效需求依据')
                excluded.add(exclusion['scenario_id'])
            output_ids = {row['id'] for row in result['items']}
            if not {row['id'] for row in context['cases'] if row['scenario_id'] not in excluded} <= output_ids:
                raise DomainError('评审遗漏了未被排除的已有用例；原草稿已保留')
            if context.get('selected_scope') and not output_ids <= {row['id'] for row in context['cases']}:
                raise DomainError('评审修改超出了选中的用例；原内容已保留')
        if task == 'complete_case_fields':
            missing = {row['id']: set(row['fields']) for row in context['missing']}
            rows = result['items']
            if len(rows) != len(missing) or {row['id'] for row in rows} != missing.keys():
                raise DomainError('模板补全遗漏或重复了选中的用例')
            by_id = {row['id']: row for row in context['cases']}
            for row in rows:
                if set(row['fields']) - missing[row['id']]:
                    raise DomainError('模板补全尝试改写已有或人工字段；原内容已保留')
                if any(filled(value) for value in row['fields'].values()) and (
                        not row['refs'] or not set(row['refs']) <= set(by_id[row['id']]['refs'])):
                    raise DomainError('模板补全值缺少当前用例的有效证据引用')

    def _validate(self, kind, rows, evidence, parents=(), profile=None):
        refs = {e['id']: e for e in evidence}
        scenarios = next((a for a in parents if a['type'] == 'scenarios'), None)
        analysis = next((a for a in parents if a['type'] == 'analysis'), None)
        scenario_ids = {r['id'] for r in scenarios['items']} if scenarios is not None else None
        legacy = scenarios.get('_legacy_unlinked_cases', {}) if scenarios is not None else {}
        validate_items(kind, rows, refs,
            scenario_ids | set(legacy.values()) if scenario_ids is not None and kind == 'cases' else None)
        if kind == 'cases' and scenario_ids is not None:
            for row in rows:
                if row['scenario_id'] not in scenario_ids and not independent_item(kind, row) and (row['id'] not in legacy or legacy[row['id']] != row['scenario_id']):
                    raise DomainError('新增或重新关联的用例必须属于当前场景；历史导入关联只保留原条目')
        if kind == 'analysis' and any(any(k in r for k in ('steps', 'scenario_id', 'preconditions')) for r in rows):
            raise DomainError('需求理解包含用例字段；当前结果尚未保存')
        if kind == 'scenarios':
            allowed = {r['id'] for r in analysis['items']} if analysis else None
            for row in rows:
                ids = row.get('requirement_ids')
                if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids) or (not ids and not independent_item(kind, row)):
                    raise DomainError('场景必须关联具体需求编号，或由用户明确设为 N/A')
                if allowed is not None and not set(ids) <= allowed:
                    raise DomainError('场景关联了不属于当前需求理解的编号')
        if kind == 'cases':
            requirement_ids = {r['id'] for r in analysis['items']} if analysis else None
            for row in rows:
                ids = row.get('requirement_ids')
                direct = (row.get('_independent_origin') or {}).get('mode') == 'direct_requirements'
                if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids)):
                    raise DomainError('用例关联需求编号必须为字符串数组')
                if direct and (not ids or row.get('scenario_id') != ''):
                    raise DomainError('直接生成的用例需关联明确需求，并将场景标为 N/A')
                if ids and requirement_ids is not None and not set(ids) <= requirement_ids:
                    raise DomainError('用例关联了不属于当前需求理解的编号')
            allowed = (profile or {}).get('case_types', ['Business', 'Negative', 'Boundary'])
            if any(r['type'] not in allowed for r in rows):
                raise DomainError('用例类型超出了当前 Profile 的范围')

    def _merge_results(self, results):
        rows, report = [], {'summary': '', 'questions': [], 'question_suggestions': [], 'assumptions': []}
        for value in results:
            rows.extend(value['items'])
            part = _report(value)
            report['summary'] += ('\n' if report['summary'] else '') + part.pop('summary', '')
            for key, content in part.items():
                if key not in report:
                    report[key] = content
                elif isinstance(report[key], list):
                    report[key].extend(content if isinstance(content, list) else [content])
                elif report[key] != content:
                    # Extension fields can legitimately differ in shape by batch.
                    # Preserve each value; typed core arrays still concatenate.
                    report[key] = [report[key], *(content if isinstance(content, list) else [content])]
        for key in ('questions', 'assumptions'):
            report[key] = list(dict.fromkeys(report[key]))
        return rows, report

    def _suggestions(self, report, evidence):
        valid_refs = {e['id'] for e in evidence if e.get('role') != 'example'}
        supplied = {question_key(s.get('question')): s for s in report.get('question_suggestions', []) if isinstance(s, dict)}
        report['questions'] = [q['question'] for q in pending_questions(report)]
        result = []
        for question in report.get('questions', []):
            value = copy.deepcopy(supplied.get(question_key(question), {}))
            supported = (value.get('confidence') == 'supported' and bool(value.get('refs'))
                         and set(value['refs']) <= valid_refs)
            value.update(question=question,
                answer=value.get('answer') or f'建议暂不对「{question}」中尚未明确的规则作通过/失败断言；保留待核实条件，先覆盖已经确认的行为。',
                basis=value.get('basis') or '这是待确认的测试设计假设，并非已经确认的业务规则。',
                confidence='supported' if supported else 'assumption', refs=value.get('refs') if supported else [])
            if not supported and '假设' not in value['basis']:
                value['basis'] += '（未确认假设）'
            options = question_options(value.pop('options', None))
            if options:
                value['options'] = options
            result.append(value)
        report['question_suggestions'] = result
        return report

    def _save(self, run, kind, rows, report, guard, old=None, sources=None, roles=None, *, artifact_key=None):
        sources = sources if sources is not None else run['_source_ids']
        from .conversation_facts import report_provenance
        report = report_provenance(self.store, run['chat_id'], report, rows, guard.get('sources', []))
        with _writes(), self.store.transaction():
            deps.assert_manifest(self.store, guard)
            current = self._run(run)
            current['_source_ids'] = list(dict.fromkeys(current['_source_ids'] + list(sources)))
            current['_source_roles'] = {**current.get('_source_roles', {}), **(roles or {})}
            self.store.save_run(current)
            if old:
                artifact = self.store.revise_artifact(old['id'], old['revision'], rows,
                    reason='native_generation', report=report, source_ids=sources,
                    source_roles=roles, dependencies=guard, provenance=guard)
            else:
                artifact = self.store.artifact(run['id'], artifact_key or 'native:artifact:' + kind, kind,
                    {'analysis': '需求理解', 'scenarios': '测试场景', 'cases': '测试用例'}[kind],
                    rows, report, dependencies=guard, provenance=guard)
            artifact['_visible'] = True
            self.store.put('artifact', artifact)
            if artifact_key:
                self.store.cache_set(run['id'], 'native:artifact:' + kind,
                                     {'id': artifact['id'], 'revision': artifact['revision']})
            current = self._run(run)
            current['artifact_ids'] = list(dict.fromkeys(current.get('artifact_ids', []) + [artifact['id']]))
            self.store.save_run(current)
            return artifact

    async def understand(self, run):
        run = self._run(run)
        sources, roles, evidence = self._evidence(run)
        business = [e for e in evidence if e.get('role') != 'example']
        if not business:
            raise DomainError('请上传需求文档或在对话中提供具体需求；示例用例仅用于格式学习')
        old = self._existing(run, 'analysis')
        guard = self._manifest(sources)
        digest = deps.digest({'sources': guard, 'instruction': run.get('_request', {}).get('content'), 'profile': run['_profile']})
        if old and old.get('report', {}).get('_native_input_digest') == digest:
            return old
        build = lambda group: self._context(run, group, previous_items=old['items'] if old else [])
        results = await self._groups('understand_requirements', business, build, rows_schema('analysis'),
            load_prompt('business.understand') + load_prompt('analysis.diagrams'), run)
        rows, report = self._merge_results(results)
        # Parallel evidence partitions must not share accidentally reused local IDs.
        seen = set()
        for row in rows:
            if row['id'] in seen:
                row['id'] = row['id'] + '-' + deps.digest(row)[:8]
            seen.add(row['id'])
        if not rows:
            raise DomainError('没有找到可生成测试的业务需求；请补充功能规则')
        self._validate('analysis', rows, evidence)
        self._suggestions(report, evidence)
        complete_analysis_diagrams(report, rows, previous=old, whole_response=len(results) == 1)
        report['_native_input_digest'] = digest
        report['source_coverage'] = {'processed_evidence_ids': [e['id'] for e in business],
            'processed_chunks': len(business), 'total_chunks': len(business)}
        return self._save(run, 'analysis', rows, report, guard, old, sources, roles)

    def _affected(self, old, parent):
        current = _rows_digest(parent['items'])
        lineage = (old or {}).get('report', {}).get('lineage', {})
        previous = lineage.get('parent_item_hashes', {})
        if old and lineage.get('parent_rules_hash') != deps.digest(_global_rules(parent)):
            return current.keys() | previous.keys()
        return {key for key in current.keys() | previous.keys() if current.get(key) != previous.get(key)}

    async def _downstream(self, run, kind, parent, analysis=None, *, direct=False):
        run, parent = self._run(run), self._artifact(parent)
        analysis = self._artifact(analysis) if analysis else parent
        parents = [parent] if kind == 'scenarios' or direct else [analysis, parent]
        old = self._existing(run, kind)
        if old and kind == 'cases':
            run = {**run, '_profile': artifact_profile(old, run['_profile'])}
        legacy = (old or {}).get('report', {}).get('_legacy_unlinked_cases', {}) if kind == 'cases' else {}
        if legacy:
            parent['_legacy_unlinked_cases'] = copy.deepcopy(legacy)
        retained_sources = list(parent.get('_source_ids') or []) + list((old or {}).get('_source_ids') or [])
        retained_roles = {**(old or {}).get('_source_roles', {}), **parent.get('_source_roles', {})}
        sources, roles, evidence = self._evidence(run, retained_sources, retained_roles)
        guard = self._manifest(sources, parents)
        mode_changed = bool(direct and old and old.get('report', {}).get('lineage', {}).get('generation_mode') != 'direct_requirements')
        changed = {r['id'] for r in parent['items']} if mode_changed else self._affected(old, parent)
        if old and kind == 'cases' and not direct:
            lineage = old.get('report', {}).get('lineage', {})
            before, after = lineage.get('analysis_item_hashes', {}), _rows_digest(analysis['items'])
            changed_requirements = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
            changed |= {row['id'] for row in parent['items'] if set(row.get('requirement_ids', [])) & changed_requirements}
            if lineage.get('analysis_rules_hash') != deps.digest(_global_rules(analysis)):
                changed |= {row['id'] for row in parent['items']}
        if old and not changed:
            return old
        parent_rows = [r for r in parent['items'] if r['id'] in changed]
        relation = (lambda r: set(r.get('requirement_ids', []))) if kind == 'scenarios' or direct else (lambda r: {r['scenario_id']})
        previous_rows = [r for r in (old or {}).get('items', []) if relation(r) & changed]
        unaffected = [copy.deepcopy(r) for r in (old or {}).get('items', []) if not relation(r) & changed]
        if mode_changed:
            previous_rows, unaffected = [], []
        previous_by_id = {r['id']: r for r in previous_rows}
        # A row tied to multiple requirements must receive all of those requirements.
        if kind == 'scenarios' or (direct and not mode_changed):
            required_ids = changed | {rid for r in previous_rows for rid in r['requirement_ids']}
            parent_rows = [r for r in parent['items'] if r['id'] in required_ids]
            regenerated = {r['id'] for r in parent_rows}
            previous_rows = [r for r in (old or {}).get('items', []) if relation(r) & regenerated]
            unaffected = [copy.deepcopy(r) for r in (old or {}).get('items', []) if not relation(r) & (regenerated | changed)]
            previous_by_id = {r['id']: r for r in previous_rows}
        def build(group):
            ids = {r['id'] for r in group}
            relevant = [r for r in previous_rows if relation(r) & ids]
            refs = {ref for r in group + relevant for ref in r.get('refs', [])}
            fields = {'analysis' if kind == 'scenarios' or direct else 'scenarios': group,
                      'previous_items': relevant, 'parent_rules': _global_rules(parent)}
            if direct:
                fields.update(scenarios=[], generation_mode='direct_requirements',
                              understanding_rules=_global_rules(analysis))
            elif kind == 'cases':
                required = {rid for row in group for rid in row.get('requirement_ids', [])}
                fields['analysis'] = [r for r in analysis['items'] if r['id'] in required]
                fields['understanding_rules'] = _global_rules(analysis)
                refs.update(ref for row in fields['analysis'] for ref in row.get('refs', []))
            selected_evidence = [e for e in evidence if e['id'] in refs or e.get('role') == 'example']
            return self._context(run, selected_evidence, **fields)
        schema = rows_schema(kind, run['_profile'])
        if direct:
            item_schema = schema['properties']['items']['items']
            item_schema['required'].append('requirement_ids')
            item_schema['properties']['requirement_ids'] = {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}}
        relationship_instruction = (load_prompt('business.generate_direct_cases') if direct else
            load_prompt('business.generate_relationships'))
        results = await self._groups('generate_scenarios' if kind == 'scenarios' else 'generate_cases',
            parent_rows, build, schema,
            relationship_instruction + load_prompt('business.generate_rows'), run) if parent_rows else []
        generated, report = self._merge_results(results)
        existing_parent_ids = {r['id'] for r in parent['items']}
        required_existing = {r['id'] for r in previous_rows if relation(r) & existing_parent_ids}
        if not required_existing <= {r['id'] for r in generated}:
            raise DomainError('更新遗漏了仍然有效的已有条目；未保存本次结果')
        generated = [{**previous_by_id.get(r['id'], {}), **r} for r in generated]
        if kind == 'cases':
            generated = _case_fields(generated, run['_profile'], previous_rows)
        if len({r['id'] for r in unaffected + generated}) != len(unaffected + generated):
            raise DomainError('模型复用了其他条目的稳定编号；未保存可能覆盖已有内容的结果')
        by_id = {r['id']: r for r in unaffected + generated}
        rows = [by_id.pop(r['id']) for r in (old or {}).get('items', []) if r['id'] in by_id]
        rows.extend(by_id.values())
        self._validate(kind, rows, evidence, parents, run['_profile'])
        covered = {rid for row in rows for rid in relation(row)}
        if not existing_parent_ids <= covered:
            raise DomainError('下游结果没有覆盖所有输入条目；未保存不完整结果')
        if old and kind == 'cases' and isinstance(old.get('report', {}).get('table_columns'), list):
            report['table_columns'] = copy.deepcopy(old['report']['table_columns'])
        prefix = 'analysis' if kind == 'scenarios' or direct else 'scenario'
        report['lineage'] = {prefix + '_artifact_id': parent['id'], prefix + '_revision': parent['revision'],
            'parent_item_hashes': _rows_digest(parent['items']), 'parent_rules_hash': deps.digest(_global_rules(parent))}
        if direct:
            report['lineage']['generation_mode'] = 'direct_requirements'
        if legacy:
            report['_legacy_unlinked_cases'] = copy.deepcopy(legacy)
        if kind == 'cases':
            report['lineage'].update(analysis_artifact_id=analysis['id'], analysis_revision=analysis['revision'],
                analysis_item_hashes=_rows_digest(analysis['items']), analysis_rules_hash=deps.digest(_global_rules(analysis)))
            rows = await self._complete_rows(run, rows, run['_profile'], evidence, run=run)
            report['template_check'] = template_check(run['_profile'], rows)
        self._suggestions(report, evidence)
        # Switching generation strategies creates a separate branch so manual
        # values on existing scenario-based cases are never silently replaced.
        if mode_changed:
            return self._save(run, kind, rows, report, guard, None, sources, roles,
                              artifact_key='native:artifact:cases:direct_requirements')
        return self._save(run, kind, rows, report, guard, old, sources, roles)

    async def scenarios(self, run, analysis):
        return await self._downstream(run, 'scenarios', analysis)

    async def cases(self, run, analysis, scenarios):
        if not self._artifact(scenarios)['items']:
            raise DomainError('当前没有可生成用例的场景；请先添加场景')
        return await self._downstream(run, 'cases', scenarios, analysis)

    async def direct_cases(self, run, analysis):
        if not self._artifact(analysis)['items']:
            raise DomainError('当前没有可生成用例的需求，请先补充需求')
        return await self._downstream(run, 'cases', analysis, analysis, direct=True)

    def _parents(self, artifact):
        lineage = artifact.get('report', {}).get('lineage', {})
        parents = [self._artifact(lineage[key]) for key in ('analysis_artifact_id', 'scenario_artifact_id') if lineage.get(key)]
        for parent in parents:
            if (parent['chat_id'], parent['project_id']) != (artifact['chat_id'], artifact['project_id']):
                raise DomainError('上游成果不属于当前对话', 404)
            if parent['type'] == 'scenarios':
                parent['_legacy_unlinked_cases'] = copy.deepcopy(artifact.get('report', {}).get('_legacy_unlinked_cases', {}))
        return parents

    async def review(self, run, cases, *, preview=False, feedback=""):
        run, cases = self._run(run), self._artifact(cases)
        run = {**run, '_profile': artifact_profile(cases, run['_profile'])}
        if not cases['items']:
            raise DomainError('当前没有可评审的测试用例；请先添加或生成用例')
        request = run.get('_request', {})
        selected_ids = request.get('selected_ids') if (request.get('artifact_id') == cases['id']
                       or request.get('intent') == 'review_case') else None
        sources, roles, evidence = self._evidence(cases)
        parents = self._parents(cases)
        guard = self._manifest(sources, parents + [cases])
        previous_proposal = None
        if feedback and run.get('review_proposal_id'):
            candidate = self.store.get('review_proposal', run['review_proposal_id'])
            if candidate['artifact_id'] == cases['id'] and candidate['artifact_revision'] == cases['revision']:
                previous_proposal = candidate
        feedback_scope = run.get('_review_feedback_scope') or {}
        if feedback and feedback_scope.get('proposal_id') == run.get('review_proposal_id') and (
                feedback_scope.get('artifact_id') != cases['id']
                or feedback_scope.get('artifact_revision') != cases['revision'] or not previous_proposal):
            raise DomainError('评审反馈引用的用例或建议版本已改变；原建议已保留', 409)
        scoped_feedback = bool(feedback and previous_proposal and
            feedback_scope.get('proposal_id') == previous_proposal['id'] and
            feedback_scope.get('artifact_id') == cases['id'] and
            feedback_scope.get('artifact_revision') == cases['revision'])
        # The scope belongs to this one proposal, not to the original generation
        # request. Once the graph installs a new proposal this scope expires.
        if scoped_feedback:
            selected_ids = feedback_scope.get('selected_ids')
        draft_rows = copy.deepcopy(previous_proposal['items']) if scoped_feedback else copy.deepcopy(cases['items'])
        if scoped_feedback and selected_ids is not None:
            # A selected deletion can be revised using its original saved row.
            draft_ids = {row['id'] for row in draft_rows}
            draft_rows.extend(copy.deepcopy(row) for row in cases['items']
                              if row['id'] in selected_ids and row['id'] not in draft_ids)
        draft = {**cases, 'items': draft_rows}
        selected = self._selection(draft, selected_ids)
        selected_set = {row['id'] for row in selected}
        untouched = [row for row in draft_rows if row['id'] not in selected_set]
        previous_report = copy.deepcopy((previous_proposal or {}).get('report', {}))
        previous_reviews = previous_report.get('review_reports') or []
        previous_review = previous_reviews[-1] if previous_reviews else previous_report

        def scoped_issues(issues, ids, *, outside=False):
            kept = []
            for issue in issues:
                if not isinstance(issue, dict) or not issue.get('case_ids'):
                    kept.append(copy.deepcopy(issue))
                    continue
                remaining = [item_id for item_id in issue['case_ids']
                             if (item_id not in ids if outside else item_id in ids)]
                if remaining:
                    kept.append({**copy.deepcopy(issue), 'case_ids': remaining})
            return kept

        def build(group):
            refs = {ref for r in group for ref in r.get('refs', [])}
            scenario_ids = {r['scenario_id'] for r in group}
            return self._context(run, [e for e in evidence if e['id'] in refs or e.get('role') == 'example'],
                cases=group, previous_items=group,
                selected_scope=selected_ids is not None, review_feedback=feedback,
                previous_review=({
                    'report': {**{key: previous_review[key] for key in ('summary', 'questions') if key in previous_review},
                        'issues': scoped_issues(previous_review.get('issues', []), {item['id'] for item in group})},
                    'items': [row for row in previous_proposal['items'] if row['id'] in {item['id'] for item in group}]
                } if previous_proposal else None),
                legacy_unlinked_cases=cases.get('report', {}).get('_legacy_unlinked_cases', {}),
                generation_mode=cases.get('report', {}).get('lineage', {}).get('generation_mode'),
                analysis=[r for a in parents if a['type'] == 'analysis' for r in a['items']
                          if r['id'] in {rid for row in group for rid in row.get('requirement_ids', [])}],
                scenarios=[r for a in parents if a['type'] == 'scenarios' for r in a['items'] if r['id'] in scenario_ids])
        results = await self._groups('review_cases', selected, build, rows_schema('cases', run['_profile']),
            load_prompt('business.review'), run)
        rows, review = self._merge_results(results)
        if selected_ids is not None and any(isinstance(issue, dict) and issue.get('case_ids')
                and not set(issue['case_ids']) <= selected_set for issue in review.get('issues', [])):
            raise DomainError('评审意见引用了未选中的用例；原建议已保留', 409)
        old = {r['id']: r for r in selected}
        rows = [{**old.get(r['id'], {}), **r} for r in rows]
        rows = _case_fields(rows, run['_profile'], selected)
        if selected_ids is not None and not {row['id'] for row in rows} <= selected_set:
            raise DomainError('评审修改超出了选中的用例；原内容已保留')
        self._validate('cases', rows, evidence, parents, run['_profile'])
        evidence_map = {e['id']: e for e in evidence if e.get('role') != 'example'}
        excluded = set()
        for exclusion in review.get('excluded_scenarios', []):
            if not exclusion.get('reason', '').strip() or not exclusion.get('refs') or not set(exclusion['refs']) <= evidence_map.keys():
                raise DomainError('评审排除缺少理由或有效需求依据')
            excluded.add(exclusion['scenario_id'])
        if any(row['scenario_id'] in excluded for row in untouched):
            raise DomainError('该场景仍有未选中的用例，不能在局部评审中排除整个场景；请查看评审范围')
        required = {r['id'] for r in selected if r['scenario_id'] not in excluded}
        if not required <= {r['id'] for r in rows}:
            raise DomainError('评审遗漏了未被排除的已有用例；原草稿已保留')
        rows = await self._complete_rows(run, rows, run['_profile'], evidence, run=run)
        reviewed = {row['id']: row for row in rows}
        if set(reviewed) & {row['id'] for row in untouched}:
            raise DomainError('评审返回的新增编号与未选中用例重复，原内容已保留')
        rows = [reviewed.pop(row['id']) if row['id'] in reviewed else copy.deepcopy(row)
                for row in draft_rows if row['id'] in reviewed or row['id'] not in selected_set]
        rows.extend(reviewed.values())
        review['scope'] = {'case_ids': sorted(selected_set), 'all': selected_ids is None,
                           'reviewed_count': len(selected_set), 'total_count': len(draft_rows)}
        report_base = previous_report if scoped_feedback else copy.deepcopy(cases.get('report', {}))
        if scoped_feedback and selected_ids is not None:
            retained = scoped_issues(previous_review.get('issues', []), selected_set, outside=True)
            # Keep the previous proposal's untouched opinions visible in the latest
            # review panel; deduplicate global notes the model repeats verbatim.
            review['issues'] = retained + [issue for issue in review.get('issues', []) if issue not in retained]
            selected_scenarios = {row['scenario_id'] for row in selected}
            preserved_exclusions = [copy.deepcopy(entry) for entry in report_base.get('excluded_scenarios', [])
                                    if entry.get('scenario_id') not in selected_scenarios]
            review['excluded_scenarios'] = preserved_exclusions + [entry for entry in review.get('excluded_scenarios', [])
                                                                     if entry not in preserved_exclusions]
            review['retained_case_ids'] = [row['id'] for row in untouched]
        reports = copy.deepcopy(report_base.get('review_reports', [])) if selected_ids is not None else []
        reports.append(review)
        report = {**copy.deepcopy(report_base), 'review_reports': reports,
                  'template_check': template_check(run['_profile'], rows), 'excluded_scenarios': review.get('excluded_scenarios', [])}
        if preview:
            from .review_proposals import save_review_proposal
            return save_review_proposal(self.store, run, cases, rows, report, guard, sources, roles, feedback)
        return self._save(run, 'cases', rows, report, guard, cases, sources, roles)

    async def propose_review(self, run, cases, feedback=''):
        return await self.review(run, cases, preview=True, feedback=feedback)

    def apply_review_proposal(self, run, proposal_id):
        from .review_proposals import require_current_review
        run = self._run(run)
        with self.store.transaction():
            proposal = require_current_review(self.store, run['id'], proposal_id)
            if self._run(run)['status'] == 'cancelled':
                raise DomainError('任务已取消', 409)
            if proposal['status'] == 'applied':
                return self.store.revision(proposal['artifact_id'], proposal['applied_revision'])
            cases = self._artifact(proposal['artifact_id'])
            resolution = self.store.get('table_review_resolution', proposal['_resolution_id']) if proposal.get('_resolution_id') else proposal
            if resolution.get('artifact_id') != cases['id'] or resolution.get('artifact_revision') != cases['revision']:
                raise DomainError('评审选择对应的用例版本已改变', 409)
            result = self._save(run, 'cases', resolution['items'], resolution['report'],
                resolution['_dependencies'], cases, resolution['_source_ids'], resolution['_source_roles'])
            if proposal.get('_resolution_id'):
                from .table_review import _receipt
                _receipt(self.store, self.store.get('chat', run['chat_id']), resolution['prompt_id'], result)
            from .storage import now
            self.store.put('review_proposal', {**proposal, 'status': 'applied',
                           'applied_revision': result['revision'], 'applied_at': now()})
            return result

    async def _complete_rows(self, holder, rows, profile, evidence, run=None, retry_unresolved=False):
        rows = materialize_fields(rows, profile)
        gaps = [g for g in template_check(profile, rows)['missing'] if retry_unresolved or not g['reason']]
        if not gaps:
            return rows
        columns = {c['field']: c for c in template_columns(profile)}
        missing = {}
        for gap in gaps:
            missing.setdefault(gap['id'], set()).add(gap['field'])
        target = [r for r in rows if r['id'] in missing]
        selected_holder = {**holder, '_profile': profile}
        def build(group):
            refs = {ref for row in group for ref in row.get('refs', [])}
            return self._context(selected_holder, [e for e in evidence if e['id'] in refs],
                cases=group, missing=[{'id': row['id'], 'fields': sorted(missing[row['id']])} for row in group])
        results = await self._groups('complete_case_fields', target, build,
            completion_schema(sorted({field for fields in missing.values() for field in fields})),
            load_prompt('business.complete_fields'), run)
        by_id = {row['id']: row for row in rows}
        handled = set()
        refs_by_id = {row['id']: set(row.get('refs', [])) for row in target}
        for result in results:
            for item in result['items']:
                item_id = item['id']
                if item_id in handled or item_id not in missing:
                    raise DomainError('模板补全返回了重复或未选中的用例；原内容已保留')
                handled.add(item_id)
                if set(item['fields']) - missing[item_id]:
                    raise DomainError('模板补全尝试改写已有或人工字段；原内容已保留')
                unresolved = {value['field']: value['reason'] for value in item['unresolved']}
                if not set(unresolved) <= missing[item_id]:
                    raise DomainError('模板补全原因不属于本次缺失字段')
                if any(filled(value) for value in item['fields'].values()) and (
                        not item['refs'] or not set(item['refs']) <= refs_by_id[item_id]):
                    raise DomainError('模板补全值缺少当前用例的有效证据引用')
                row = by_id[item_id]
                for field in missing[item_id]:
                    value = item['fields'].get(field)
                    if filled(value):
                        row[field] = copy.deepcopy(value)
                        row.get('_template_field_notes', {}).pop(field, None)
                    else:
                        reason = unresolved.get(field, '').strip()
                        row.setdefault('_template_field_notes', {})[field] = {
                            'reason': reason or '当前需求和用例依据不足，暂未生成该字段，请补充具体业务规则。',
                            'contract': column_signature(columns[field])}
        if handled != missing.keys():
            raise DomainError('模板补全遗漏了选中的用例；原内容已保留')
        return materialize_fields(rows, profile)

    async def complete_fields(self, artifact, profile=None, ids=None):
        """Fill missing template fields without changing the artifact's frozen Profile."""
        artifact = self._artifact(artifact)
        if artifact['type'] != 'cases':
            raise DomainError('只有测试用例可以补全模板字段')
        profile_record = profile if isinstance(profile, dict) and 'config' in profile else None
        if profile_record and profile_record.get('project_id') != artifact['project_id']:
            raise DomainError('Profile 不属于当前项目')
        config = copy.deepcopy(profile_record['config'] if profile_record else profile or artifact_profile(artifact))
        selected = self._selection(artifact, ids)
        sources, roles, evidence = self._evidence(artifact)
        guard = deps.manifest(self.store, source_ids=sources,
            artifact_ids=[{'id': artifact['id'], 'revision': artifact['revision']}],
            profile_ids=[profile_record['id']] if profile_record else [])
        completed = await self._complete_rows(artifact, selected, config, evidence, retry_unresolved=True)
        changed = {row['id']: row for row in completed}
        rows = [changed.get(row['id'], copy.deepcopy(row)) for row in artifact['items']]
        if rows == artifact['items']:
            return artifact
        self._validate('cases', rows, evidence, self._parents(artifact), artifact_profile(artifact))
        report = copy.deepcopy(artifact.get('report', {}))
        report['template_check'] = template_check(artifact_profile(artifact), rows)
        report['template_completion'] = template_check(config, rows)
        with _writes():
            return self.store.revise_artifact(artifact['id'], artifact['revision'], rows,
                reason='native_field_completion', report=report, dependencies=guard, provenance=guard)

    def _selection(self, artifact, ids):
        if ids is not None and (not ids or not set(ids) <= {r['id'] for r in artifact['items']}):
            raise DomainError('所选条目不属于当前成果')
        return [copy.deepcopy(r) for r in artifact['items'] if ids is None or r['id'] in ids]

    async def revise(self, artifact, ids=None, instruction='', new_values=None, source_ids=None, source_roles=None,
                     preview=False, dialogue_content=None, add=False, parent_id=None, independent=False, delete=False):
        artifact = self._artifact(artifact)
        if delete:
            if add or new_values is not None or independent or not ids:
                raise DomainError('删除行需要明确选择条目，且不能同时新增、修改字段或解除关联')
            selected = self._selection(artifact, ids)
            selected_ids = {row['id'] for row in selected}
            rows = [copy.deepcopy(row) for row in artifact['items'] if row['id'] not in selected_ids]
            sources, roles, evidence = self._evidence(artifact, source_ids, source_roles)
            parents = self._parents(artifact)
            self._validate(artifact['type'], rows, evidence, parents, artifact_profile(artifact))
            report = copy.deepcopy(artifact.get('report', {}))
            if artifact['type'] == 'cases':
                report['template_check'] = template_check(artifact_profile(artifact), rows)
            if artifact['type'] == 'analysis':
                complete_analysis_diagrams(report, rows, previous=artifact)
            report.pop('_native_input_digest', None)
            return {'artifact_id': artifact['id'], 'base_revision': artifact['revision'], 'items': rows,
                    'report': report, 'source_ids': sources, 'source_roles': roles,
                    'dependencies': self._manifest(sources, parents + [artifact])}
        selected = self._selection(artifact, ids)
        selected_ids = {r['id'] for r in selected}
        relation_field = 'requirement_ids' if artifact['type'] == 'scenarios' else 'scenario_id'
        relation_empty = [] if artifact['type'] == 'scenarios' else ''
        explicit_empty = isinstance(new_values, dict) and new_values.get(relation_field, None) == relation_empty
        independent_edit = artifact['type'] in ('scenarios', 'cases') and (independent or explicit_empty)
        if independent and parent_id:
            raise DomainError('独立条目不能同时指定上游编号，请选择关联或 N/A')
        sources, roles, evidence = self._evidence(artifact, source_ids, source_roles)
        parents = self._parents(artifact)
        dialogue = None
        guard_parents = parents
        if dialogue_content is not None:
            from .dialogue_lineage import prepare_dialogue
            dialogue = prepare_dialogue(self.store, artifact, dialogue_content, add=add, parent_id=parent_id)
            parents, guard_parents = dialogue['parents'], dialogue['original_parents']
            parent_sources = [sid for p in guard_parents for sid in p.get('_source_ids', [])]
            parent_roles = {sid: role for p in guard_parents for sid, role in p.get('_source_roles', {}).items()}
            sources, roles, evidence = self._evidence(artifact, list(source_ids or []) + parent_sources,
                {**parent_roles, **(source_roles or {})})
            evidence += dialogue['evidence']
        if add and (dialogue is None or new_values is not None):
            raise DomainError('新增条目需要当前用户的具体要求，字段直接赋值仅用于已有条目')
        guard = self._manifest(sources, guard_parents + [artifact])
        diagram_response_is_whole = False
        if new_values is not None:
            if not isinstance(new_values, dict) or set(new_values) & {'id', 'report', '_source_ids'} or any(k.startswith('_') for k in new_values):
                raise DomainError('只能修改业务字段，不能改变稳定编号或内部记录')
            revised = [{**r, **copy.deepcopy(new_values)} for r in selected]
            report = copy.deepcopy(artifact.get('report', {}))
        else:
            def build(group):
                refs = {ref for row in group for ref in row.get('refs', [])}
                context_parents = parents
                if add:
                    parent_id = dialogue['addition_parent_id']
                    scenario_rows = [r for p in parents if p['type'] == 'scenarios' for r in p['items'] if r['id'] == parent_id]
                    requirement_ids = {rid for r in scenario_rows for rid in r.get('requirement_ids', [])}
                    if artifact['type'] == 'scenarios':
                        requirement_ids.add(parent_id)
                    context_parents = [{**p, 'items': [r for r in p['items'] if r['id'] in
                        (requirement_ids if p['type'] == 'analysis' else {parent_id})]} for p in parents]
                    refs = {ref for p in context_parents for r in p['items'] for ref in r.get('refs', [])}
                    if ids is not None:
                        refs.update(ref for r in selected for ref in r.get('refs', []))
                if dialogue and dialogue['addition_parent_id']:
                    refs.update(ref for parent in parents for row in parent['items']
                                if row['id'] == dialogue['addition_parent_id'] for ref in row.get('refs', []))
                incoming = set(source_ids or [])
                if dialogue:
                    incoming.add(dialogue['source']['id'])
                relevant = [e for e in evidence if e['id'] in refs or e['source_id'] in incoming or e.get('role') == 'example']
                return self._context({**artifact, '_profile': artifact_profile(artifact)}, relevant, artifact_type=artifact['type'], items=[] if add else group,
                    previous_items=[] if add else group, instruction=instruction,
                    existing_item_ids=[r['id'] for r in artifact['items']] if add else [],
                    existing_items=selected if add and ids is not None else [],
                    addition_parent_id=dialogue['addition_parent_id'] if dialogue else None,
                    dialogue_evidence_ids=[e['id'] for e in dialogue['evidence']] if dialogue else [],
                    legacy_unlinked_cases=dialogue.get('legacy_unlinked_cases', {}) if dialogue else artifact.get('report', {}).get('_legacy_unlinked_cases', {}),
                    add_only=add, independent_addition=bool(add and not dialogue['addition_parent_id']),
                    independent_item_ids=sorted(selected_ids) if independent_edit and not add else [],
                    independent_edit=independent_edit and not add,
                    independent_reason='用户明确设置为 N/A' if independent_edit else '用户独立新增，不关联上游成果',
                    independent_source_id=dialogue['source']['id'] if dialogue else None,
                    report={} if add else _report({'report': artifact.get('report', {})}),
                    **{a['type']: a['items'] for a in context_parents})
            # An addition is one requested work item; never repeat it once per old-row capacity batch.
            inputs = [{'id': 'dialogue-addition', 'refs': []}] if add else selected
            results = await self._groups('revise_artifact', inputs, build,
                rows_schema(artifact['type'], artifact_profile(artifact)),
                load_prompt('business.revise')
                + (load_prompt('business.revise_add') if add else '')
                + (load_prompt('business.revise_independent')
                   if independent_edit else load_prompt('business.revise_linked'))
                + (load_prompt('analysis.diagrams') if artifact['type'] == 'analysis' else ''))
            diagram_response_is_whole = not add and len(results) == 1 and len(selected) == len(artifact['items'])
            revised, new_report = self._merge_results(results)
            supplied_report_fields = {key for result in results for key in result.get('report', {})}
            for key in ('questions', 'question_suggestions', 'assumptions'):
                if key not in supplied_report_fields:
                    new_report.pop(key, None)
            if not add and not selected_ids <= {r['id'] for r in revised}:
                raise DomainError('修改遗漏了选定条目，原内容已经保留')
            if ids is not None and not add and {r['id'] for r in revised} != selected_ids:
                raise DomainError('修改超出了所选条目范围')
            original = {r['id']: r for r in selected}
            if add:
                all_existing = {r['id'] for r in artifact['items']}
                if any(r['id'] in all_existing for r in revised):
                    raise DomainError('新增返回复用了已有条目编号；原成果已保留')
                added = [r for r in revised if r['id'] not in all_existing]
                if not added:
                    raise DomainError('模型未返回新增条目，请补充具体希望增加的行为')
                for row in added:
                    relation = row.get('requirement_ids') if artifact['type'] == 'scenarios' else row.get('scenario_id')
                    expected_relation = ([dialogue['addition_parent_id']] if dialogue['addition_parent_id'] else []) if artifact['type'] == 'scenarios' else (dialogue['addition_parent_id'] or '')
                    if artifact['type'] in ('scenarios', 'cases') and relation != expected_relation:
                        raise DomainError('新增条目未使用指定关联；未指定上游时请使用 N/A 空关联')
                    row['refs'] = list(dict.fromkeys(row.get('refs', []) + [e['id'] for e in dialogue['evidence']]))
                    row['_dialogue_origin'] = {'source_id': dialogue['source']['id'],
                                              'kind': 'dialogue_supplement', 'label': '用户对话补充'}
                revised = [copy.deepcopy(r) for r in selected] + added
            revised = [{**original.get(r['id'], {}), **r} for r in revised]
            if artifact['type'] == 'cases':
                revised = _case_fields(revised, artifact_profile(artifact), selected)
            report = {**copy.deepcopy(artifact.get('report', {})), **new_report}
        from .dialogue_lineage import normalize_independent_rows
        revised = normalize_independent_rows(artifact['type'], revised, artifact['items'],
            '用户明确设置为 N/A' if independent_edit else '用户独立新增，不关联上游成果',
            authorized_ids=selected_ids if independent_edit and not add else [],
            allow_new=bool(add and dialogue and not dialogue['addition_parent_id']),
            source_id=dialogue['source']['id'] if dialogue else None, force=independent_edit and not add)
        changes = {r['id']: r for r in revised}
        rows = [changes.pop(r['id'], copy.deepcopy(r)) for r in artifact['items']] + list(changes.values())
        if dialogue and not add:
            original_rows = {r['id']: r for r in artifact['items']}
            for row in rows:
                if row != original_rows.get(row['id']):
                    row['refs'] = list(dict.fromkeys(row.get('refs', []) + [e['id'] for e in dialogue['evidence']]))
                    row['_dialogue_origin'] = {'source_id': dialogue['source']['id'],
                        'kind': 'dialogue_supplement', 'label': '用户对话补充'}
        self._validate(artifact['type'], rows, evidence, parents, artifact_profile(artifact))
        self._suggestions(report, evidence)
        if artifact['type'] == 'analysis':
            complete_analysis_diagrams(report, rows, previous=artifact,
                whole_response=diagram_response_is_whole or rows == artifact['items'])
        if artifact['type'] == 'cases':
            report['template_check'] = template_check(artifact_profile(artifact), rows)
        report.pop('_native_input_digest', None)
        if dialogue:
            from .dialogue_lineage import link_report
            added_ids = {r['id'] for r in rows} - {r['id'] for r in artifact['items']}
            parent_ids = {rid for r in rows if r['id'] in added_ids for rid in
                          (r.get('requirement_ids', []) if artifact['type'] == 'scenarios' else [r.get('scenario_id')])}
            requirement_ids = {rid for p in parents if p['type'] == 'scenarios'
                               for r in p['items'] if r['id'] in parent_ids for rid in r.get('requirement_ids', [])}
            report = link_report(report, artifact['type'], parents, parent_ids, requirement_ids,
                existing_items=artifact['items'], new_item_ids=added_ids)
            if dialogue.get('legacy_unlinked_cases'):
                report['_legacy_unlinked_cases'] = copy.deepcopy(dialogue['legacy_unlinked_cases'])
        from .conversation_facts import report_provenance
        report = report_provenance(self.store, artifact['chat_id'], report, rows, guard.get('sources', []))
        proposal = {'artifact_id': artifact['id'], 'base_revision': artifact['revision'],
            'items': rows, 'report': report, 'source_ids': sources, 'source_roles': roles,
            'dependencies': guard}
        if dialogue:
            proposal['dialogue'] = dialogue
        if preview or dialogue:
            return proposal
        return self.apply_revision_preview(proposal)

    def apply_revision_preview(self, proposal):
        if proposal.get('dialogue'):
            from .dialogue_lineage import commit_dialogue
            return commit_dialogue(self, proposal)
        artifact = self._artifact(proposal['artifact_id'])
        sources, roles, evidence = self._evidence(artifact, proposal['source_ids'], proposal['source_roles'])
        self._validate(artifact['type'], proposal['items'], evidence, self._parents(artifact), artifact_profile(artifact))
        with _writes():
            return self.store.revise_artifact(artifact['id'], proposal['base_revision'], proposal['items'],
                reason='native_tool_edit', report=proposal['report'], source_ids=sources, source_roles=roles,
                dependencies=proposal['dependencies'], provenance=proposal['dependencies'])

    async def clarify(self, run, analysis, answers):
        run, analysis = self._run(run), self._artifact(analysis)
        from .documents import parse_text
        from .project_context import share_clarification
        questions = pending_questions(analysis.get('report'))
        content, resolved = submitted_answers(questions, answers)
        if not content.strip():
            raise DomainError('请提供或采用具体澄清答案')
        text, chunks = parse_text(content)
        # The source identity is durable even if a model request subsequently fails.
        key = 'native:clarification:' + deps.digest({'analysis': analysis['id'], 'content': content,
            'shared': run.get('clarification_save_to_project', True)})
        saved = self.store.cache_get(run['id'], key)
        if saved:
            source = self.store.get('source', saved['source_id'])
        else:
            source = self.store.add_source(run['chat_id'], '已确认的项目澄清', 'clarification', text, chunks)
            if run.get('clarification_save_to_project', True):
                share_clarification(self.store, source['id'], run['project_id'])
            self.store.cache_set(run['id'], key, {'source_id': source['id']})
        proposal = await self.revise(analysis, instruction='依据这些已确认答案更新需求理解，保留稳定编号。已回答的问题从 questions 移除。\n' + content,
                                    source_ids=[source['id']], source_roles={source['id']: 'clarification'}, preview=True)
        report = proposal['report']
        unanswered = [q['question'] for q in questions if q['question'] not in resolved]
        previously_resolved = analysis.get('report', {}).get('_clarification_resolved_questions', [])
        resolved_questions = list(dict.fromkeys(previously_resolved + list(resolved)))
        known = {question_key(q['question']) for q in questions} | {question_key(q) for q in resolved_questions}
        # A model may echo old questions or introduce followups. Neither can reopen
        # an answered gate or become a confirmed business fact without user input.
        followups = list(report.get('clarification_followups', []))
        followups += [q['question'] for q in pending_questions(report) if question_key(q['question']) not in known]
        followups = [q for q in followups if question_key(q) not in known]
        if followups:
            report['clarification_followups'] = list(dict.fromkeys(followups))
        else:
            report.pop('clarification_followups', None)
        report['_clarification_resolved_questions'] = resolved_questions
        report['questions'] = unanswered
        # Keep the original proposal for questions the user has not answered yet.
        report.setdefault('question_suggestions', []).extend(s for s in analysis.get('report', {}).get('question_suggestions', [])
            if isinstance(s, dict) and question_key(s.get('question')) in {question_key(q) for q in unanswered})
        _, _, evidence = self._evidence(analysis, [source['id']], {source['id']: 'clarification'})
        self._suggestions(report, evidence)
        revised = self.apply_revision_preview(proposal)
        with self.store.transaction():
            current = self._run(run)
            current['_source_ids'] = list(dict.fromkeys(current['_source_ids'] + [source['id']]))
            current['_source_roles'] = {**current.get('_source_roles', {}), source['id']: 'clarification'}
            self.store.save_run(current)
        return revised

    apply_clarification = clarify

    async def estimate(self, artifact, ids=None):
        artifact = self._artifact(artifact)
        from .conversation_facts import ensure_artifact_allowed
        ensure_artifact_allowed(self.store, artifact)
        if artifact['type'] != 'scenarios':
            raise DomainError('请指定场景成果进行用例数量估算')
        selected = self._selection(artifact, ids)
        results = await self._groups('estimate_workload', selected,
            lambda group: {'scenarios': group, 'profile': artifact.get('_profile', {})}, ESTIMATE_SCHEMA,
            load_prompt('business.estimate'))
        rows = [r for result in results for r in result['scenarios']]
        if len(rows) != len(selected) or {r['scenario_id'] for r in rows} != {r['id'] for r in selected}:
            raise DomainError('估算没有准确覆盖所选场景')
        if any(type(r['min_count']) is not int or type(r['max_count']) is not int or r['min_count'] < 0 or r['max_count'] < r['min_count'] for r in rows):
            raise DomainError('估算范围无效')
        return {'artifact_id': artifact['id'], 'artifact_revision': artifact['revision'],
            'summary': '\n'.join(r['summary'] for r in results), 'scenarios': rows,
            'min_count': sum(r['min_count'] for r in rows), 'max_count': sum(r['max_count'] for r in rows)}

    async def analyze(self, artifact, instruction, ids=None):
        artifact = self._artifact(artifact)
        selected = self._selection(artifact, ids)
        # Explaining saved content is read-only. Use its captured evidence even
        # after project eligibility changes; never reintroduce it into a run.
        guard = artifact.get('_write_dependencies') or artifact.get('_dependencies') or {}
        references = guard.get('sources', [])
        if references and all(isinstance(ref, dict) and ref.get('version') for ref in references):
            evidence = [entry for ref in references for entry in self.store.evidence_version(
                ref['id'], ref['version'], artifact.get('_source_roles', {}).get(ref['id']))]
        else:
            _, _, evidence = self._evidence(artifact)
        chat = self.store.get('chat', artifact['chat_id'])
        historical_rules = (artifact.get('report', {}).get('_knowledge_preference_version', 1)
                            != chat.get('_project_knowledge_version', 1))
        if historical_rules:
            instruction += '\nThis is a historical artifact explanation. Its rules are not current project eligibility; do not propose them as active requirements or new generation inputs.'
        def build(group):
            refs = {ref for row in group for ref in row.get('refs', [])}
            return self._context(artifact, [e for e in evidence if e['id'] in refs],
                                 artifact_type=artifact['type'], items=group, instruction=instruction)
        results = await self._groups('explain_artifact', selected, build, ANSWER_SCHEMA,
            load_prompt('business.explain'))
        allowed = {e['id'] for e in evidence if e.get('role') != 'example'}
        refs = list(dict.fromkeys(ref for result in results for ref in result.get('refs', [])))
        if not set(refs) <= allowed:
            raise DomainError('回答引用了未提供的依据')
        answer = '\n\n'.join(r['answer'] for r in results)
        if historical_rules:
            answer = '以下说明基于该成果保存时的历史内容，不表示其中规则仍适用于本次生成。\n' + answer
        return {'answer': answer, 'refs': refs}
