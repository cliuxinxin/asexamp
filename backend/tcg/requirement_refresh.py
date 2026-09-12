"""Apply submitted clarification to the current understanding without regenerating it."""
import copy
import json

from .artifact_actions import evidence_for, modify_draft
from .context_service import analysis_signature
from .dependencies import assert_manifest, digest, manifest
from .schemas import DomainError


def report_presentation(analysis):
    """Bound optional authoring context without weakening the rule ContextPack.

    Only complete, small presentation fields are copied. Requirement maps and
    reconciled rules stay in artifact_context's scoped mandatory projection.
    """
    report = analysis.get('report', {})
    fields, omitted = {}, []
    for key in ('summary', 'questions', 'strategy', 'diagrams'):
        if key not in report:
            continue
        candidate = {**fields, key: report[key]}
        if len(json.dumps(candidate, ensure_ascii=False)) <= 1400:
            fields[key] = copy.deepcopy(report[key])
        else:
            omitted.append(key)
    omitted.extend(key for key in ('requirement_map', 'relationships', 'global_rules', 'conflicts',
        'assumptions', 'in_scope', 'out_of_scope') if key in report)
    return {'fields': fields, 'included_fields': list(fields), 'omitted_fields': omitted,
        'partial': bool(omitted), 'scope': 'Optional complete presentation fields; business rules use the task global_rules projection.'}


REPORT_PATCH_GUIDANCE = ('报告上下文是有界展示投影，未提供的字段不表示不存在。保留未完整提供的报告字段，'
    '不得用局部片段整体替换 requirement_map 或其他完整报告；'
    '必须遵守任务 global_rules 中实际提供的业务规则及其证据，只定向修订有完整依据的内容。')


async def refresh_clarification(engine, run_id, analysis, source_id, answer):
    """Refresh rows and report once for this submitted answer; return the saved head.

    The workflow has already accepted the answer. Uploading a source by itself never
    invokes this helper. Stable operation receipts make checkpoint replay harmless.
    """
    store = engine.store
    if not isinstance(answer, str) or not answer.strip():
        raise DomainError('澄清回答不能为空')
    key = 'v280:clarification_refresh:' + digest([analysis['id'], source_id, answer])
    with store.transaction():
        run = store.run(run_id)
        current = store.get('artifact', analysis['id'])
        if current['type'] != 'analysis' or any(current[k] != run[k] for k in ('project_id', 'chat_id')):
            raise DomainError('需求理解必须属于当前澄清任务')
        source = store.get('source', source_id)
        if source['project_id'] != run['project_id'] or not source.get('_active') or source['role'] != 'clarification':
            raise DomainError('澄清更新需要当前项目中已保存的澄清依据')
        if store.cache_get(run_id, key) or key in current.get('report', {}).get('clarification_refreshes', {}):
            return current
        store.assert_running(run_id)
        ids, roles, evidence = evidence_for(store, [current], [source_id])
        guard = manifest(store, artifact_ids=[current['id']], source_ids=ids, run_id=run_id)
    report_context = report_presentation(current)
    instruction = ('用户已提交以下澄清，请依据这份澄清及现有需求定向更新当前需求理解。'
        '返回局部 operations，不重新分析或整体重写。保持现有稳定 ID、无关需求、人工修改和自定义字段；'
        '只在澄清明确新增且现有需求尚未表达时增加带该澄清引用的新需求，并避免重复。'
        '更新需要变化的摘要、业务图、requirement_map、关联规则、范围、假设或策略作为 report_patch；不得凭空补充业务规则。'
        '保留仍未解决的问题，不再重复已回答的问题。\n已提交的问题与回答：\n' + answer
        + '\n' + REPORT_PATCH_GUIDANCE + '\n可用展示字段：\n' + json.dumps(report_context, ensure_ascii=False))
    draft, change, _ = await modify_draft(engine, current, {'instruction': instruction,
        'source_ids': [source_id], 'allow_additions': True}, evidence, store)
    report = copy.deepcopy(change.get('report', current.get('report', {})))
    remaining = report.get('questions', [])
    if remaining == current.get('report', {}).get('questions', []):
        remaining = []
    if not isinstance(remaining, list) or any(not isinstance(question, str) for question in remaining):
        raise DomainError('剩余澄清问题必须为文本数组')
    report.update(clarification=answer, previous_questions=copy.deepcopy(current.get('report', {}).get('questions', [])),
        questions=remaining, question_suggestions=[],
        clarification_note='已结合用户提交的澄清定向更新需求理解；保留已有条目及人工修改。',
        confirmed_requirements=copy.deepcopy(draft['items']))
    report['clarification_refreshes'] = {**report.get('clarification_refreshes', {}), key: {
        'source_id': source_id, 'source_refs': [e['id'] for e in evidence if e['source_id'] == source_id],
        'answer_digest': digest(answer), 'base_revision': current['revision'],
        'revision': current['revision'] + 1, 'diff': copy.deepcopy(change['diff'])}}
    merged_ids = list(dict.fromkeys(run.get('_source_ids', []) + ids))
    merged_roles = {**run.get('_source_roles', {}), **roles}
    report['analysis_signature'] = analysis_signature(store, merged_ids, merged_roles,
        run.get('_profile', {}), run.get('scope'))
    with store.transaction():
        assert_manifest(store, guard)
        updated = store.revise_artifact(current['id'], current['revision'], draft['items'],
            reason='apply_clarification', run_id=run_id, cache_key=key, report=report,
            source_ids=ids, source_roles=roles, dependencies=guard, provenance=change.get('_provenance'))
        active = store.run(run_id)
        if active.get('_source_ids') != merged_ids or active.get('_source_roles') != merged_roles:
            version = max(active.get('input_version', 0), active.get('_input_version', 0)) + 1
            store.update_run(run_id, _source_ids=merged_ids, _source_roles=merged_roles,
                input_version=version, _input_version=version)
        store.cache_set(run_id, 'v6:requirement_map', {**updated['report'], 'confirmed_requirements': updated['items']})
        store.cache_set(run_id, 'workspace:analysis_parent', {'id': updated['id'], 'revision': updated['revision']})
        return updated
