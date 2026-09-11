"""Capacity-bounded read-only dialogue for legacy Run entry points.

The caller owns persistence. This module never edits artifacts, confirmations or
Run stop policy. Model invocation retains the existing ``dialogue`` task.
"""
import copy
import hashlib
import json
import re

from .context_budget import request_budget
from .context_service import artifact_context, _terms
from .dependencies import manifest
from .schemas import DomainError


def _fits(engine, context):
    budget = request_budget(engine.store.directory, 'dialogue', context, engine.settings)
    return budget['fits'] and (not hasattr(engine, 'fits') or engine.fits('dialogue', context))


def _groups(engine, values, build):
    groups, current = [], []
    for value in values:
        try:
            fits = _fits(engine, build(current + [value]))
        except DomainError:
            # The shared builder may reject a combined mandatory span budget.
            # A failing singleton is retried below and its actual error propagates.
            fits = False
        if fits:
            current.append(value)
            continue
        if current:
            groups.append(current)
        current = [value]
        if not _fits(engine, build(current)):
            raise DomainError('单个解释条目及其必要证据超过模型容量；请缩小问题范围或调整实际模型容量，未截断原文。')
    if current or not groups:
        if not _fits(engine, build(current)):
            raise DomainError('对话指令及必要上下文超过模型容量，请缩小提问范围。')
        groups.append(current)
    return groups


def _base(run, content, pending):
    request = {key: run['_request'].get(key) for key in ('content', 'intent', 'mode')}
    if content is not None:
        request.update(content=content, intent='query')
    history = run.get('_conversation', [])
    conversation = [{'role': m.get('role'), 'content': str(m.get('content', ''))[:400]} for m in history[-4:]]
    profile = run.get('_profile', {})
    gate = {key: copy.deepcopy(pending[key]) for key in (
        'type', 'node', 'artifact_id', 'revision', 'artifact_revision', 'interrupt_id', 'control_version', 'reason') if key in pending}
    if isinstance(pending.get('message'), str):
        gate['message'] = pending['message'][:400]
    return {'request': request, 'profile': {k: copy.deepcopy(profile[k]) for k in ('language', 'scope') if k in profile},
        'conversation': conversation,
        'conversation_context': {'available_messages': len(history), 'partial': len(history) > 4 or any(len(str(m.get('content', ''))) > 400 for m in history[-4:])},
        'pending_confirmation': gate,
        'instruction': '只解释当前提供的成果、业务证据或系统流程，不修改成果或推进确认。业务事实必须引用本批精确证据ID；未提供的内容不得假装已覆盖。回答保持简洁。'}


def _validate(result, refs):
    if not isinstance(result, dict) or not isinstance(result.get('answer'), str) or not result['answer'].strip():
        raise DomainError('对话回答格式无效，请重新提问；当前确认节点保留。')
    returned = result.get('refs', [])
    if not isinstance(returned, list) or any(not isinstance(r, str) or r not in refs for r in returned):
        raise DomainError('对话引用不属于本批提供的证据，请重新提问；当前确认节点保留。')
    return {'answer': result['answer'], 'refs': list(dict.fromkeys(returned))}


async def answer_dialogue(engine, run_id, content=None, pending=None):
    """Return ``{answer, refs, coverage}``; callers retain save/confirmation guards."""
    store = engine.store
    run = store.run(run_id)
    pending = (pending if pending is not None else run.get('interrupt', {})) or {}
    base = _base(run, content, pending or {})
    question = base['request'].get('content') or ''
    artifact = copy.deepcopy(run.get('_artifact_snapshot'))
    if pending.get('artifact_id'):
        revision = pending.get('artifact_revision', pending.get('revision'))
        artifact = store.revision(pending['artifact_id'], revision) if revision is not None else store.get('artifact', pending['artifact_id'])
    if artifact and any(artifact.get(k) != run.get(k) for k in ('project_id', 'chat_id')):
        raise DomainError('对话成果不属于当前资料范围', 409)
    source_ids = list(dict.fromkeys(run.get('_source_ids', []) + (artifact or {}).get('_source_ids', [])))
    roles = {**(artifact or {}).get('_source_roles', {}), **run.get('_source_roles', {})}
    evidence = [e for e in store.evidence(source_ids, roles) if e.get('role') != 'example']
    selected_ids = run['_request'].get('selected_ids')
    if artifact and artifact.get('type') in ('analysis', 'scenarios', 'cases'):
        rows = artifact.get('items', [])
        if selected_ids is not None:
            available = {r['id'] for r in rows}
            if not set(selected_ids) <= available:
                raise DomainError('选中的成果条目已改变，请重新选择', 409)
            rows = [r for r in rows if r['id'] in selected_ids]
        def build(values):
            projected = {**artifact, '_profile': copy.deepcopy(base['profile'])}
            context = artifact_context(store, 'dialogue', projected, values, evidence, instruction=base['instruction'] + '\n' + question,
                explicit_source_ids=run['_request'].get('source_ids') or [], extra=base, admit=False)
            return {**context, **copy.deepcopy(base)}
        values = rows
    else:
        terms = _terms(question)
        broad = bool(re.search(r'总结|概述|汇总|全部|所有|整体|summari[sz]e|overview|all requirements', question, re.I))
        values = [e for e in evidence if broad or e.get('role') == 'clarification' or terms & _terms(e.get('text', ''))]
        values.sort(key=lambda e: (-len(terms & _terms(e.get('text', ''))), e['id']))
        def build(selected):
            return {**copy.deepcopy(base), 'evidence': copy.deepcopy(selected), 'artifact': None,
                'dependency_manifest': manifest(store, source_ids=list(dict.fromkeys(e['source_id'] for e in selected))),
                'coverage': {'available_evidence_count': len(evidence), 'included_evidence_count': len(selected),
                    'partial': len(selected) < len(evidence)}}
    groups = _groups(engine, values, build)
    results, included, missing, missing_versions = [], set(), set(), []
    for group in groups:
        context = build(group)
        refs = {e['id'] for e in context['evidence']}
        included.update(refs)
        missing.update(context.get('coverage', {}).get('missing_referenced_evidence_ids', []))
        for version in context.get('coverage', {}).get('missing_historical_source_versions', []):
            if version not in missing_versions:
                missing_versions.append(version)
        result = await engine.invoke_model('dialogue', context, run_id)
        results.append(_validate(result, refs))
    available = {e['id'] for e in evidence}
    coverage = {'selected_row_ids': [r['id'] for r in values] if artifact and artifact.get('type') in ('analysis', 'scenarios', 'cases') else [],
        'available_evidence_count': len(available), 'included_evidence_count': len(included),
        'included_evidence_ids': sorted(included), 'omitted_evidence_ids': sorted(available - included),
        'missing_referenced_evidence_ids': sorted(missing), 'batch_count': len(groups),
        'missing_historical_source_versions': missing_versions,
        'partial': bool(available - included or missing or missing_versions),
        'note': '仅对实际提供的条目和证据作答；分批说明不代表已执行测试或自动确认。'}
    if len(results) == 1:
        return {**results[0], 'coverage': coverage}
    # Final synthesis only consumes bounded derived answers, never full sources,
    # artifact reports, or artifact suites. Any excerpt is explicitly disclosed.
    compact_coverage = {k: v for k, v in coverage.items() if k.endswith('_count') or k in ('partial', 'note')}
    compact_coverage['evidence_digest'] = hashlib.sha256(json.dumps(sorted(included)).encode()).hexdigest()
    summary_base = {k: copy.deepcopy(base[k]) for k in ('request', 'profile', 'pending_confirmation')}
    summary_base.update(dialogue_phase='summary', evidence=[], artifact=None, coverage=compact_coverage,
        instruction='只汇总 answers 中已经完成的分批解释，不重新分析原文，不增加事实。保留冲突、不确定性和覆盖限制；引用只能来自 answers.refs。用简洁回答概括，不声称执行测试或推进确认。')
    while True:
        compact = [{**r, 'answer': r['answer'][:1600], 'answer_excerpt': len(r['answer']) > 1600} for r in results]
        if any(r['answer_excerpt'] for r in compact):
            coverage['answer_excerpts'] = True
            summary_base['coverage']['answer_excerpts'] = True
        def summary_build(answers):
            return {**copy.deepcopy(summary_base), 'answers': copy.deepcopy(answers)}
        summary_groups = _groups(engine, compact, summary_build)
        next_results = []
        for group in summary_groups:
            result = await engine.invoke_model('dialogue', summary_build(group), run_id)
            next_results.append(_validate(result, {ref for answer in group for ref in answer['refs']}))
        if len(next_results) == 1:
            return {**next_results[0], 'coverage': coverage}
        if len(next_results) >= len(results):
            raise DomainError('分批解释的汇总仍超过模型容量，请缩小提问范围；原成果和确认节点保留。')
        results = next_results
