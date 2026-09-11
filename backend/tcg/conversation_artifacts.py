"""Conversation capabilities over saved artifact snapshots and validated proposals."""
import copy
import json
import re

from .artifact_actions import (apply_action, bounded_groups, changed_requirement_ids,
    evidence_for, local_evidence, preview_action, selected_rows, snapshot_action, visible_artifact)
from .conversation_receipts import assert_command_live, commit_result
from .model import TASK_INSTRUCTIONS
from .schemas import DomainError
from .storage import now, public


TASK_INSTRUCTIONS.update({
    'artifact_sync_scenarios': '''Return {operations:[{op:"add|update|delete",id:"target ID",item:{id:"stable ID",field:"new value"},reason:"required for deletion",refs:["evidence IDs required for deletion"]}],summary:"changes"}. Synchronize ONLY supplied scenarios to supplied NEW analysis drafts. selected_requirement_ids defines affected requirements. update/delete only selected_ids; additions require complete scenario fields id/title/description/priority/refs/requirement_ids and must link supplied requirements. Keep stable IDs and custom/manual fields. Preserve unrelated requirement links. Delete obsolete scenarios only with specific reason and supplied non-example evidence refs. Ensure every remaining selected requirement has scenario coverage. Do not generate cases. Return operations, never a full replacement set.''',
    'artifact_review_readonly': '''Review the supplied saved cases WITHOUT modifying them. Return {report:{summary:"assessment",issues:[{id:"supplied case ID",description:"concrete finding",refs:["provided evidence IDs"]}],coverage:["coverage observation"],refs:["provided evidence IDs"]}}. Describe design quality, step/expected clarity and evidence-grounded gaps. Preserve actual IDs. Do not return operations, new cases, patches or claims that tests ran. Review does not save case revisions. Respect user scope and language.''',
    'artifact_analyze_sources': '''Return {answer:"answer to current instruction",refs:["exact supplied evidence IDs"]}. Answer only from the supplied current project sources and shared confirmed clarification evidence. Preserve confirmed values and identify uncertainty or disagreement. Do not generate or modify artifacts, infer missing requirements or claim testing occurred. Business facts must reference supplied non-example evidence. If evidence is insufficient explain what is missing.''',
    'artifact_compare': '''Return {answer:"comparison requested",refs:["provided evidence IDs"]}. Compare only the supplied saved artifact snapshots and explicit revisions. Explain actual differences and stable IDs. Never infer changes to unprovided artifacts, generate cases, modify data or claim testing occurred.''',
})
TASK_INSTRUCTIONS['artifact_modify'] += ' Cases may patch ONLY review_reports in report_patch when the user explicitly edits saved review wording; review artifacts may patch summary/issues/coverage/score/limitations. Report patches never change lineage or execution records.'


def _schema(description, **properties):
    return {'type': 'object', 'description': description, 'properties': properties, 'additionalProperties': False}


_TARGET = {
    'artifact_id': {'type': 'string', 'description': 'Resolved saved artifact ID.'},
    'expected_revision': {'type': 'integer', 'description': 'Resolved current revision required for writes.'},
    'artifact_revision': {'type': 'integer', 'description': 'Alias for expected_revision.'},
    'selected_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Exact saved row IDs; omitted means all rows.'},
    'instruction': {'type': 'string', 'default': '', 'description': 'User request including limits and requested presentation.'},
    'question': {'type': 'string', 'description': 'Alias for instruction.'},
    'detail': {'type': 'string', 'description': 'Presentation preference such as steps; saved steps are always included for cases.'},
    'source_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional new project evidence source IDs.'},
}
_EDIT = {**_TARGET,
    'related_artifact_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Explicit descendant scope; scenario IDs alone stop before cases.'},
    'sync_related': {'type': 'boolean', 'default': False, 'description': 'Propagate from the new upstream draft in one atomic commit.'},
    'preview': {'type': 'boolean', 'default': False, 'description': 'Return a proposal only when the user explicitly requested a preview.'},
    'action': {'type': 'string', 'enum': ['modify', 'sync'], 'default': 'modify', 'description': 'Preview modification or downstream synchronization.'},
}
CAPABILITIES = {
    'artifact.read': {'effect': 'read', 'description': 'Display saved content, including each actual case step and expected result.', 'parameters': _schema('Read a saved version.', **_TARGET)},
    'artifact.analyze': {'effect': 'read', 'description': 'Explain, summarize, compare or inspect saved artifacts; without an artifact answer from current project sources and shared facts.', 'parameters': _schema('Read-only artifact or project analysis.', **_TARGET,
        artifact_ids={'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional saved artifacts for comparison.'},
        compare_revision={'type': 'integer', 'description': 'Compare target to this historical revision.'})},
    'artifact.estimate': {'effect': 'read', 'description': 'Estimate case counts for selected scenarios without generating cases or advancing a workflow.', 'parameters': _schema('Scenario design estimate.', **_TARGET)},
    'artifact.coverage': {'effect': 'read', 'description': 'Show actual requirement/scenario/case links, missing coverage and stale versions.', 'parameters': _schema('Structural coverage.', **_TARGET,
        case_artifact_id={'type': 'string', 'description': 'Explicit case branch to include.'})},
    'artifact.revise': {'effect': 'write', 'description': 'Save an explicit scoped edit immediately while preserving the current workflow confirmation.', 'parameters': _schema('Validated edit.', **_EDIT)},
    'artifact.preview': {'effect': 'write', 'description': 'Create a scoped modification preview without changing artifacts; requires later apply.', 'parameters': _schema('Explicit preview.', **_EDIT)},
    'artifact.apply': {'effect': 'write', 'description': 'Apply the identified preview after checking all upstream versions and evidence.', 'parameters': _schema('Apply a proposal.', **_TARGET, proposal_id={'type': 'string', 'description': 'Exact pending proposal ID.'})},
    'artifact.discard': {'effect': 'control', 'description': 'Cancel only the identified modification preview, preserving workflow and artifacts.', 'parameters': _schema('Discard a proposal.', **_TARGET, proposal_id={'type': 'string', 'description': 'Exact pending proposal ID.'})},
    'artifact.sync_related': {'effect': 'write', 'description': 'Synchronize scoped descendants using current upstream content; preview only when requested.', 'parameters': _schema('Scoped downstream propagation.', **_EDIT)},
    'artifact.review_cases': {'effect': 'read', 'description': 'Review existing cases without modifying or generating them and display their actual steps.', 'parameters': _schema('Read-only case review.', **_TARGET, optimize={'type': 'boolean', 'default': False, 'description': 'Only true when the user explicitly authorizes reviewing and modifying cases.'})},
}


from .capability_contracts import contract, BUSINESS_TARGETS, prepare_review
for _name, _definition in CAPABILITIES.items():
    CAPABILITIES[_name] = contract(_definition,
        target_types=() if _name in ('artifact.apply', 'artifact.discard') else
            ('scenarios',) if _name == 'artifact.estimate' else
            ('cases',) if _name == 'artifact.review_cases' else BUSINESS_TARGETS,
        target_required=_name not in ('artifact.analyze', 'artifact.apply', 'artifact.discard'),
        context_policy='artifact_evidence',
        prepare=prepare_review if _name == 'artifact.review_cases' else None)
CAPABILITIES['artifact.review_cases']['description'] += ' optimize:true explicitly authorizes saving validated case improvements; otherwise review is read-only.'


def _result(message, parts=(), status='succeeded', pending=None):
    result = {'status': status, 'message': message, 'parts': list(parts)}
    if pending is not None:
        result['pending'] = pending
    return result


def _scoped(store, chat, artifact_id):
    artifact = visible_artifact(store, artifact_id)
    if artifact['project_id'] != chat['project_id'] or artifact['chat_id'] != chat['id']:
        raise DomainError('成果不属于当前对话', 404)
    return copy.deepcopy(artifact)


def _validate_answer(value, evidence):
    known = {e['id'] for e in evidence}
    if not isinstance(value, dict) or not isinstance(value.get('answer'), str) or not value['answer'].strip():
        raise DomainError('分析输出需要有效回答')
    refs = value.get('refs', [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in known for ref in refs):
        raise DomainError('回答包含无效引用')
    return {'type': 'answer', 'text': value['answer'], 'refs': list(dict.fromkeys(refs))}


def relevant_evidence(evidence, instruction, sources, max_chunks=12, max_chars=24000):
    """Small lexical retrieval, with confirmed project facts ranked first."""
    lowered = instruction.lower()
    stop = {'what', 'the', 'this', 'that', 'from', 'with', 'please', 'project', '当前', '本项', '项目', '多少', '什么', '确认', '请问', '保存'}
    terms = set(re.findall(r'[a-z0-9_]{2,}', lowered)) - stop
    for phrase in re.findall(r'[\u3400-\u9fff]+', lowered):
        for size in (2, 3):
            terms.update(phrase[i:i + size] for i in range(len(phrase) - size + 1) if phrase[i:i + size] not in stop)
    def score(row):
        text = (row.get('text', '') + ' ' + sources[row['source_id']].get('name', '')).lower()
        matches = sum(min(text.count(term), 4) * len(term) for term in terms)
        shared = sources[row['source_id']].get('_project_shared', False)
        return matches + (10 if shared else 0), matches, shared
    ranked = sorted(enumerate(evidence), key=lambda pair: (score(pair[1])[0], -pair[0]), reverse=True)
    chosen = [index for index, row in ranked if score(row)[1] or score(row)[2]][:max_chunks]
    if not chosen:
        chosen = [index for index, _ in ranked[:min(3, max_chunks)]]
    # Include immediate neighbors only when there is spare capacity and a real match.
    anchors = list(chosen)
    for index in anchors:
        if not score(evidence[index])[1]:
            continue
        for neighbor in (index - 1, index + 1):
            if 0 <= neighbor < len(evidence) and neighbor not in chosen and evidence[neighbor]['source_id'] == evidence[index]['source_id'] and len(chosen) < max_chunks:
                chosen.append(neighbor)
    result, used, clipped = [], 0, 0
    for index in chosen:
        row = copy.deepcopy(evidence[index])
        text = str(row.get('text', ''))
        available = min(4000, max_chars - used)
        if available <= 0:
            break
        if len(text) > available:
            positions = [text.lower().find(term) for term in terms if term in text.lower()]
            start = max(0, min(positions) - available // 3) if positions else 0
            row['text'] = text[start:start + available]
            row['excerpt'] = True
            clipped += 1
        used += len(row.get('text', ''))
        result.append(row)
    return result, {'total_chunks': len(evidence), 'included_chunks': len(result),
                    'omitted_chunks': len(evidence) - len(result), 'excerpted_chunks': clipped,
                    'scope': 'Relevant retrieved evidence only; not a full-document completeness assessment.'}


async def _analyze_sources(store, engine, chat, args):
    from .project_context import shared_sources
    with store.lock:
        sources = [s for s in store.list('source', chat_id=chat['id']) if s.get('_active')]
        sources += shared_sources(store, chat['project_id'])
        overrides = store.get('chat', chat['id']).get('_source_roles', {})
        roles = {s['id']: overrides.get(s['id'], s['role']) for s in sources}
        by_id = {s['id']: {**s, 'role': roles[s['id']]} for s in sources if roles[s['id']] != 'example'}
        explicit = args.get('source_ids')
        if explicit is not None:
            if not isinstance(explicit, list) or any(not isinstance(i, str) or i not in by_id for i in explicit):
                raise DomainError('所选资料不在当前对话或项目共享范围')
            by_id = {sid: by_id[sid] for sid in explicit}
        evidence = copy.deepcopy(store.evidence(list(by_id), roles))
    if not evidence:
        return _result('当前没有可引用的已保存资料，请提供需求或已确认内容。', [{'type': 'answer', 'text': '当前没有可引用的已保存资料，请提供需求或已确认内容。', 'refs': []}])
    evidence, retrieval = relevant_evidence(evidence, args['instruction'], by_id)
    def build(rows):
        ids = {r['source_id'] for r in rows}
        return {'instruction': args['instruction'], 'evidence': rows, 'retrieval': retrieval,
                'sources': [{'id': sid, 'name': by_id[sid]['name'], 'role': by_id[sid]['role'], 'shared': bool(by_id[sid].get('_project_shared'))} for sid in sorted(ids)]}
    parts = []
    for group in bounded_groups(engine, 'artifact_analyze_sources', evidence, build):
        parts.append(_validate_answer(await engine.invoke_model('artifact_analyze_sources', build(group), None), group))
    if retrieval['omitted_chunks'] or retrieval['excerpted_chunks']:
        parts.append({'type': 'answer', 'text': f'本次回答依据相关资料片段（{retrieval["included_chunks"]}/{retrieval["total_chunks"]}）；未进行全文完整性检查。', 'refs': []})
    return _result('已根据项目资料回答。', parts)


async def _compare(store, engine, chat, artifact, args):
    ids = args.get('artifact_ids') or [artifact['id']]
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
        raise DomainError('比较成果必须为有效 ID 数组')
    snapshots = [_scoped(store, chat, aid) for aid in dict.fromkeys(ids)]
    if args.get('compare_revision') is not None:
        revision = args['compare_revision']
        if type(revision) is not int or revision < 1:
            raise DomainError('请选择有效的历史版本')
        snapshots.insert(0, store.revision(artifact['id'], revision))
    _, _, evidence = evidence_for(store, snapshots, args.get('source_ids'))
    from .context_service import artifact_context
    context = artifact_context(store, 'artifact_compare', artifact, [r for a in snapshots for r in a.get('items', [])], evidence,
        args['instruction'], args.get('source_ids') or [], {'artifacts': [public(a) for a in snapshots]})
    if len(json.dumps(context, ensure_ascii=False)) > 490000 or not engine.fits('artifact_compare', context):
        raise DomainError('比较内容超过模型容量，请缩小成果范围')
    part = _validate_answer(await engine.invoke_model('artifact_compare', context, None), context['evidence'])
    return _result('已比较所选保存版本。', [part])


def _review_report(value, rows, evidence):
    if not isinstance(value, dict) or value.get('operations'):
        raise DomainError('只读评审不能返回修改操作')
    report = value.get('report')
    if not isinstance(report, dict) or not isinstance(report.get('summary'), str) or not report['summary'].strip():
        raise DomainError('评审需要有效报告摘要')
    for key in ('issues', 'coverage'):
        if not isinstance(report.get(key, []), list):
            raise DomainError('评审问题和覆盖必须为数组')
    known, ids = {e['id'] for e in evidence}, {r['id'] for r in rows}
    def validate(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('id', 'case_id') and (not isinstance(value, str) or value not in ids):
                    raise DomainError('评审包含未提供的用例编号')
                if key == 'refs' and (not isinstance(value, list) or any(not isinstance(ref, str) or ref not in known for ref in value)):
                    raise DomainError('评审包含无效引用')
                validate(value)
        elif isinstance(node, list):
            for child in node:
                validate(child)
    validate(report)
    return report


async def _review(store, engine, artifact, args):
    if artifact['type'] != 'cases':
        raise DomainError('请指定要评审的已保存用例成果')
    rows = selected_rows(artifact, args.get('selected_ids'))
    _, _, evidence = evidence_for(store, [artifact], args.get('source_ids'))
    def build(selected):
        from .context_service import artifact_context
        return artifact_context(store, 'artifact_review_readonly', artifact, selected, evidence,
            args['instruction'], args.get('source_ids') or [], {'cases': selected}, admit=False)
    parts = []
    for group in bounded_groups(engine, 'artifact_review_readonly', rows, build):
        context = build(group)
        report = _review_report(await engine.invoke_model('artifact_review_readonly', context, None), group, context['evidence'])
        descriptions = []
        for issue in report.get('issues', []):
            if isinstance(issue, str):
                descriptions.append(issue)
            elif isinstance(issue, dict):
                descriptions.append(str(issue.get('id', issue.get('case_id', ''))) + '：' + str(issue.get('description', issue.get('message', issue.get('title', '')))))
        text = report['summary'] + ('\n\n' + '\n'.join(descriptions) if descriptions else '')
        parts.append({'type': 'answer', 'text': text, 'refs': report.get('refs', []), 'report': report})
    parts.append({'type': 'case_details', 'artifact_id': artifact['id'], 'revision': artifact['revision'], 'items': rows})
    return _result('已评审所选用例，评审意见如下。', parts)


def _applied_result(value):
    return _result('修改已保存。' + value['summary'], [{'type': 'artifact', 'artifact_id': a['id'], 'revision': a['revision']} for a in value['artifacts']])


async def execute(store, engine, chat, name, args, turn_id=None):
    """Only domain writes use commit_result inside the artifact transaction."""
    if name not in CAPABILITIES:
        raise DomainError('不支持的成果能力')
    if not isinstance(args, dict):
        raise DomainError('成果参数必须为对象')
    if turn_id:
        command = store.get('conversation_command', turn_id)
        if command.get('status') in ('succeeded', 'needs_confirmation') and command.get('result'):
            return copy.deepcopy(command['result'])
    args = copy.deepcopy(args)
    args['instruction'] = args.get('instruction') or args.get('question') or {
        'artifact.estimate': '估算这些场景需要多少条测试用例。',
        'artifact.review_cases': '评审这些用例的设计质量，不修改用例。',
        'artifact.sync_related': '同步相关成果。',
        'artifact.analyze': '总结所选已保存内容。',
    }.get(name, '')
    if not isinstance(args['instruction'], str):
        raise DomainError('操作要求必须为文本')
    if name == 'artifact.analyze' and not args.get('artifact_id') and not args.get('artifact_ids'):
        return await _analyze_sources(store, engine, chat, args)
    artifact_id = args.get('artifact_id') or (args.get('artifact_ids') or [None])[0]
    if name in ('artifact.apply', 'artifact.discard'):
        with store.transaction():
            proposal = store.get('action_proposal', args.get('proposal_id'))
            if proposal['chat_id'] != chat['id'] or proposal['project_id'] != chat['project_id'] or (artifact_id and artifact_id != proposal['artifact_id']):
                raise DomainError('预览不属于当前对话或成果', 404)
            artifact_id = proposal['artifact_id']
            assert_command_live(store, turn_id)
            if name == 'artifact.discard':
                if proposal.get('_applied'):
                    raise DomainError('修改已保存，不能作为预览取消；可提出新的修改', 409)
                store.put('action_proposal', {**proposal, '_discarded': True, '_discarded_at': now()})
                return commit_result(store, turn_id, _result('已取消这项修改预览。'))
            return commit_result(store, turn_id, _applied_result(apply_action(store, artifact_id, proposal['id'], emit_message=False)))
    if not artifact_id:
        raise DomainError('请指定要操作的已保存成果')
    with store.lock:
        artifact = _scoped(store, chat, artifact_id)
    expected = args.get('expected_revision', args.get('artifact_revision'))
    if expected is not None and (type(expected) is not int or expected < 1):
        raise DomainError('请选择有效的成果版本')
    if expected is not None and artifact['revision'] != expected:
        if CAPABILITIES[name]['effect'] == 'read' and args.get('optimize') is not True:
            artifact = store.revision(artifact_id, expected)
        else:
            raise DomainError('成果已更新，请刷新后重试', 409)
    selected_rows(artifact, args.get('selected_ids'))
    if name == 'artifact.read':
        if artifact['type'] == 'cases':
            part = {'type': 'case_details', 'artifact_id': artifact_id, 'revision': artifact['revision'], 'items': selected_rows(artifact, args.get('selected_ids'))}
        else:
            part = {'type': 'artifact', 'artifact_id': artifact_id, 'revision': artifact['revision']}
        return _result('已读取保存内容。', [part])
    if name in ('artifact.analyze', 'artifact.estimate'):
        if name == 'artifact.analyze' and (args.get('artifact_ids') or args.get('compare_revision') is not None):
            return await _compare(store, engine, chat, artifact, args)
        value = await snapshot_action(store, engine, artifact, {**args, 'action': 'estimate' if name == 'artifact.estimate' else 'explain'})
        part = {'type': 'estimate', 'data': value['estimate']} if name == 'artifact.estimate' else {'type': 'answer', 'text': value['answer'] + f'\n\n依据：{artifact["title"]} · v{artifact["revision"]}', 'refs': value['refs'], 'artifact_id': artifact_id, 'revision': artifact['revision']}
        return _result(value['summary'], [part])
    if name == 'artifact.review_cases':
        if args.get('optimize') is not True:
            return await _review(store, engine, artifact, args)
        if artifact['type'] != 'cases':
            raise DomainError('请指定要评审优化的用例成果')
        args['instruction'] = '评审并按明确要求局部优化，保留人工数据：' + args['instruction']
        name = 'artifact.revise'
    if name == 'artifact.coverage':
        from .workspace_coverage import workspace_context
        with store.lock:
            data = workspace_context(store, artifact, args.get('case_artifact_id'))
            if data.get('analysis_artifact_id') and data.get('scenario_artifact_id'):
                analysis = store.get('artifact', data['analysis_artifact_id'])
                scenarios = store.get('artifact', data['scenario_artifact_id'])
                changed = changed_requirement_ids(store, analysis, scenarios)
                data['stale']['analysis'] = bool(changed)
                data['stale']['changed_requirement_ids'] = changed
        return _result('已按保存的关联关系计算覆盖；结果不代表测试执行或业务覆盖充分。', [{'type': 'coverage', 'data': data}])
    assert_command_live(store, turn_id)
    body = {**args, 'expected_revision': artifact['revision'],
            'action': 'sync' if name == 'artifact.sync_related' else args.get('action', 'modify')}
    preview_only = name == 'artifact.preview' or args.get('preview') is True
    saved_result = None
    def on_saved(proposal):
        nonlocal saved_result
        assert_command_live(store, turn_id)
        if preview_only:
            saved_result = _result(proposal['summary'], [{'type': 'diff', 'proposal_id': proposal['id'], 'changes': proposal['changes']}],
                status='needs_confirmation', pending=[{'type': 'artifact_proposal', 'id': proposal['id'], 'proposal_id': proposal['id'], 'artifact_id': artifact_id,
                    'revision': artifact['revision'], 'label': '应用这项修改', 'actions': ['artifact.apply', 'artifact.discard']}])
            commit_result(store, turn_id, saved_result)
    proposal = await preview_action(store, engine, artifact_id, body, on_saved=on_saved)
    if preview_only:
        return saved_result
    with store.transaction():
        assert_command_live(store, turn_id)
        return commit_result(store, turn_id, _applied_result(apply_action(store, artifact_id, proposal['id'], emit_message=False)))
