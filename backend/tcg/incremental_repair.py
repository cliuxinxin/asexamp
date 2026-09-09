"""Bounded validation recovery with durable drafts and scoped reference repairs."""
import copy
import json

from .agent_repair import apply_repair, parts, repair_fragment
from .incremental_workspace import fingerprint
from .schemas import OutputValidationError


REFERENCE_RULES = {'provided_evidence_id', 'provided_evidence_ids',
                   'provided_non_example_evidence_id', 'non_example_evidence_id'}


def subject_fields(subject):
    return {k: v[:600] for k, v in subject.items()
            if k in ('title', 'description', 'label', 'reason', 'summary', 'action', 'answer') and isinstance(v, str)}


def attach_subject(result, fragment):
    parent = result
    subject = subject_fields(parent)
    for key in parts(fragment['path'])[:-1]:
        parent = parent[key]
        if isinstance(parent, dict) and subject_fields(parent):
            subject = subject_fields(parent)
    fragment['subject'] = subject
    return fragment


def reference_fragments(value, evidence, limit=8):
    """Collect existing invalid reference leaves; never change business fields."""
    fragments = []

    def visit(obj, prefix='', singular=False):
        if not isinstance(obj, dict):
            return
        for field in ('refs', 'evidence_refs'):
            child = obj.get(field)
            if isinstance(child, list):
                path = prefix + '.' + field if prefix else field
                for index, ref in enumerate(child):
                    add(ref, f'{path}[{index}]', obj)
        if singular:
            add(obj.get('ref'), prefix + '.ref', obj)

    def add(ref, path, subject):
        if len(fragments) >= limit or not isinstance(ref, str):
            return
        if ref in evidence and evidence[ref]['role'] != 'example':
            return
        error = OutputValidationError('引用不在当前可用业务证据中', path,
                                      'provided_non_example_evidence_id', ref, 'invalid_reference')
        fragment = repair_fragment(value, error.issue)
        fragment['subject'] = subject_fields(subject)
        fragments.append(fragment)

    visit(value)
    for field in ('items', 'nodes', 'edges', 'updates', 'evidence_review'):
        rows = value.get(field, [])
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                visit(row, f'{field}[{index}]', field == 'evidence_review')
    operations = value.get('operations', [])
    if isinstance(operations, list):
        for index, op in enumerate(operations):
            if isinstance(op, dict):
                visit(op.get('item'), f'operations[{index}].item')
    return fragments


def evidence_snippets(evidence, budget=5500):
    """Distribute a fixed budget across current evidence, with explicit truncation."""
    values = [e for e in evidence.values() if e['role'] != 'example']
    result = []
    share = min(600, max(0, budget // max(1, len(values)) - 180))
    for item in values:
        body = item.get('text', '')
        row = {k: item[k] for k in ('id', 'role', 'location', 'excerpt') if k in item}
        row.update(text=body[:share], truncated=len(body) > share)
        if 'excerpt' in row:
            row['excerpt'] = {**row['excerpt'], 'end': row['excerpt']['start'] + len(row['text'])}
        if len(json.dumps(result + [row], ensure_ascii=False)) > budget:
            break
        result.append(row)
    return result


def apply_batch(result, fragments, response):
    if not isinstance(response, dict) or set(response) != {'repairs'} or not isinstance(response['repairs'], list):
        raise OutputValidationError('需要返回指定字段的修复列表', 'repairs', 'exact_requested_repairs')
    expected = {f['path']: f for f in fragments}
    changes = response['repairs']
    paths = [p.get('path') for p in changes if isinstance(p, dict)]
    if len(changes) != len(expected) or len(paths) != len(expected) or any(not isinstance(p, str) for p in paths) or set(paths) != set(expected):
        raise OutputValidationError('修复列表包含遗漏、重复或越界字段', 'repairs', 'exact_requested_repairs')
    corrected = copy.deepcopy(result)
    for change in changes:
        corrected = apply_repair(corrected, expected[change['path']], change)
    return corrected


async def recover(agent, state, job, context, raw, evidence, round_number, validate):
    run_id, key, task = state['run_id'], job['key'], job['task']
    checkpoint_key = 'v3:validation:' + key + ':' + str(round_number)
    identity = fingerprint({'context': context, 'raw': raw, 'evidence': evidence, 'epoch': state.get('epoch')})
    checkpoint = agent.store.cache_get(run_id, checkpoint_key)
    if not checkpoint or checkpoint.get('identity') != identity:
        checkpoint = {'identity': identity, 'draft': copy.deepcopy(raw), 'sequence': 0, 'history': []}
    elif checkpoint['sequence']:
        agent.insight(run_id, '接续已保存的修复进度，只处理剩余错误。', [], 'decision')
    result = checkpoint['draft']
    if result.get('kind') == 'need_context':
        return result
    previous_paths, stalled, regenerated = [], 0, False

    def save():
        # Cancellation or a newer user instruction must prevent stale writes.
        with agent.store.transaction():
            agent.current(state)
            checkpoint['draft'] = copy.deepcopy(result)
            agent.store.cache_set(run_id, checkpoint_key, checkpoint)

    async def call(purpose, request):
        checkpoint['sequence'] += 1
        save()
        answer = await agent.engine.call(run_id, key + f':recovery:{round_number}:{checkpoint["sequence"]}', purpose, request)
        agent.current(state)
        return answer

    for attempt in range(13):
        try:
            accepted = validate(result)
        except OutputValidationError as exc:
            issue = exc.issue
        else:
            if checkpoint['sequence']:
                agent.insight(run_id, '当前工作项已修复并通过完整校验，继续后续流程。', [], 'finding')
                agent.engine.trace('agent.repair_complete', run_id, task=task, work_key=key)
            return accepted

        agent.engine.trace('agent.validation_failed', run_id, task=task, validation_error=issue, work_key=key)
        checkpoint['history'] = (checkpoint['history'] + [issue])[-6:]
        stalled = stalled + 1 if issue['path'] in previous_paths else 0
        save()
        if attempt == 12 or (stalled >= 2 and regenerated):
            agent.insight(run_id, '自动修复后仍无法满足 ' + issue['path'] + ' 的校验要求；修复进度和已完成成果已保存，可补充相关依据后接续。', [], 'decision')
            raise OutputValidationError('自动修复仍未解决；进度已保存，重试将接续剩余错误',
                                        issue['path'], issue['expected'], code=issue['code'])

        if stalled >= 2:
            agent.insight(run_id, '局部修复未解决问题，正在根据错误原因重新处理当前工作项；已完成的工作保留。', [], 'decision')
            extra = copy.deepcopy(context)
            extra['validation_recovery'] = {'errors': checkpoint['history'],
                'instruction': 'Previous field repairs failed. Re-derive ONLY this current unit/page from supplied facts. Copy exact evidence IDs; do not invent facts or remove required coverage. If evidence is insufficient, request focused read-only tools. Earlier accepted work is preserved by the server.'}
            # Keep the same full-prompt budget as an ordinary work call.
            extra = agent.bounded_context(agent.current(state), task, extra)
            result = await call(task, extra)
            save()
            if result.get('kind') == 'need_context':
                return result
            regenerated, stalled, previous_paths = True, 0, []
            continue

        if task == 'work_analyze' and issue['expected'] == 'all_supplied_business_evidence_analyzed':
            covered = {r for item in result.get('items', []) for r in item.get('refs', [])} | {e.get('ref') for e in result.get('evidence_review', [])}
            missing = [e for e in context['evidence'] if e['id'] not in covered]
            extra = agent.workspace.model_context(agent.current(state), task, {'evidence': missing, 'goal': '仅补齐尚未处理的段落；使用新的局部 ID。'})
            patch = await call(task, extra)
            if patch.get('kind') == 'need_context':
                return patch
            for field in ('items', 'nodes', 'edges', 'evidence_review', 'questions', 'assumptions'):
                result[field] = result.get(field, []) + patch.get(field, [])
            previous_paths = [issue['path']]
            save()
            continue

        mapped = agent.patch_issue(result, context, issue) if task in ('work_modify', 'work_review') else issue
        fragments = reference_fragments(result, evidence) if issue['expected'] in REFERENCE_RULES else []
        if not fragments:
            try:
                fragments = [attach_subject(result, repair_fragment(result, mapped, flat_fields=('questions', 'assumptions', 'techniques')))]
            except OutputValidationError:
                # A derived/container-level error cannot safely be patched as a leaf.
                previous_paths, stalled = [issue['path']], 1
                continue
        previous_paths = list(dict.fromkeys([issue['path']] + [f['path'] for f in fragments]))
        constraints = {'evidence_ids': [r for r, e in evidence.items() if e['role'] != 'example'],
            'requirement_ids': [i['id'] for i in context.get('analysis', [])],
            'branch_ids': [e['id'] for e in context.get('business_model', {}).get('edges', [])],
            'scenario_links': [{k: s[k] for k in ('id', 'requirement_ids', 'branch_ids')} for s in context.get('scenarios', [])],
            'node_ids': [n.get('id') for n in result.get('nodes', [])]}
        if task == 'work_links':
            constraints['source_units'] = [{k: u[k] for k in ('id', 'title', 'requirements', 'excerpts')} for u in context['units']]
        extra = {'original_task': task, 'constraints': constraints, 'previous_errors': checkpoint['history']}
        if issue['expected'] in REFERENCE_RULES:
            extra['evidence'] = evidence_snippets(evidence)
            extra['diagnosis'] = '引用 ID 不在当前提供的业务证据中。根据条目含义与证据原文匹配，逐字复制对应 ID；不要任选一个合法 ID。'
        batch = len(fragments) > 1
        extra['repairs' if batch else 'repair'] = fragments if batch else fragments[0]
        purpose = 'agent_repair_batch' if batch else 'agent_repair'
        if len(json.dumps(extra, ensure_ascii=False)) > 18000:
            previous_paths, stalled = [issue['path']], 1
            continue
        agent.insight(run_id, ('发现多处引用不匹配，正在结合原文集中修复 ' + str(len(fragments)) + ' 个字段。') if batch
                      else '正在结合错误原因修正字段 ' + fragments[0]['path'] + '。', [], 'decision')
        fixed = await call(purpose, extra)
        try:
            corrected = apply_batch(result, fragments, fixed) if batch else apply_repair(result, fragments[0], fixed)
            changed = corrected != result
            result = corrected
            if changed:
                agent.engine.trace('agent.repair_applied', run_id, task=task, work_key=key, field_count=len(fragments))
        except OutputValidationError as error:
            checkpoint['history'] = (checkpoint['history'] + [error.issue])[-6:]
            agent.engine.trace('agent.repair_rejected', run_id, task=task, work_key=key, validation_error=error.issue)
        save()
    raise AssertionError('unreachable')
