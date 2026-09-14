"""Bounded repair of evidence links without regenerating business rows."""
import copy

from jsonschema import Draft202012Validator

from .native_schemas import TEXT, object_schema
from .schemas import DomainError


REFERENCE_INSTRUCTION = (
    'Repair evidence references only for the supplied items. Their business fields are immutable. '
    'Choose exact IDs only from evidence, and for every selected ref quote a contiguous passage from '
    'that evidence text supporting the item. Explain the connection in reason. A parent link alone '
    'does not prove the case behavior. Never borrow a ref merely to pass validation. '
    'If current evidence cannot support the item, return refs=[] and support=[] with the reason; '
    'do not invent evidence, change the item, or omit it. Return each supplied ID exactly once.')


def submission_kind(task, context):
    return {'understand_requirements': 'analysis', 'generate_scenarios': 'scenarios',
        'generate_cases': 'cases', 'review_cases': 'cases',
        'revise_artifact': context.get('artifact_type')}.get(task)


def _issue(result, row, code, detail, **extra):
    report = result.setdefault('report', {})
    report.setdefault('issues', []).append({'title': '证据引用已处理', 'detail': detail,
        'code': code, 'item_ids': [row['id']], 'refs': copy.deepcopy(row['refs']), **extra})


def _failure(rows, call_id, reason='', *, retained=False):
    ids = [row['id'] for row in rows]
    next_step = ('可补充这些条目的业务依据，或重试本次引用修复。' if retained
                 else '请补充这些条目的业务依据后，再修改当前成果。')
    error = DomainError('证据引用仍未解决：' + '、'.join(ids) + '。原有成果及已完成批次保持不变，'
        '未发布缺少依据的条目；' + next_step + reason)
    error.category, error.call_id, error.item_ids = 'invalid_reference', call_id, ids
    return error


def _schema(ids, refs):
    ref = {'type': 'string', 'enum': refs}
    row = object_schema({'id': {'type': 'string', 'enum': ids},
        'refs': {'type': 'array', 'items': ref, 'uniqueItems': True},
        'support': {'type': 'array', 'items': object_schema({'ref': ref, 'quote': TEXT}, ['ref', 'quote'])},
        'reason': {'type': 'string', 'minLength': 1}}, ['id', 'refs', 'support', 'reason'])
    return object_schema({'items': {'type': 'array', 'items': row,
        'minItems': len(ids), 'maxItems': len(ids)}}, ['items'])


async def repair_reference_fields(call, task, context, original, save_candidate=None, diagnostics=None):
    """One native repair call per attempt; only exact grounded refs can be merged.

    Candidates are durable, unpublished data. Manual retry can reuse their valid
    rows without asking the model to regenerate a whole completed batch.
    """
    kind = submission_kind(task, context)
    if kind not in ('analysis', 'scenarios', 'cases'):
        return original
    evidence = {e['id']: e for e in context.get('evidence', []) if e.get('role') != 'example'}
    result = copy.deepcopy(original)
    rows = result.get('items')
    if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get('id'), str)
            and isinstance(r.get('refs'), list) and all(isinstance(ref, str) for ref in r['refs']) for r in rows):
        return original
    if len({row['id'] for row in rows}) != len(rows):
        return original
    pending, filtered = [], []
    for row in rows:
        valid = [ref for ref in row['refs'] if ref in evidence]
        removed = len(row['refs']) - len(valid)
        if valid and removed:
            row['refs'] = valid
            filtered.append(row['id'])
            _issue(result, row, 'reference_filtered',
                '已移除无效或示例引用，保留本批有效证据；请结合现有依据复核业务内容。', removed_count=removed)
        elif not valid and not (kind == 'analysis' and row.get('assumption') is True and not row['refs']):
            pending.append(row)
    if filtered and diagnostics:
        diagnostics.record('batch.references_filtered', task=task, call_id=getattr(original, 'call_id', None),
            item_ids=filtered)
    if not pending:
        return result
    # Save before transport so a connection failure also retains all good rows.
    if save_candidate:
        save_candidate(result)
    if not evidence:
        raise _failure(pending, getattr(original, 'call_id', None), retained=save_candidate is not None)
    ids = [row['id'] for row in pending]
    repair_context = {'artifact_type': kind, 'original_task': task, 'items': copy.deepcopy(pending),
        'evidence': list(evidence.values())}
    scenario_ids = {row.get('scenario_id') for row in pending}
    scenarios = [r for r in context.get('scenarios', []) if r['id'] in scenario_ids]
    requirement_ids = {rid for row in pending + scenarios for rid in row.get('requirement_ids', [])}
    for name, selected in (('scenarios', scenarios), ('analysis',
            [r for r in context.get('analysis', []) if r['id'] in requirement_ids])):
        if selected:
            repair_context[name] = selected
    schema = _schema(ids, list(evidence))
    if diagnostics:
        diagnostics.record('batch.reference_repair_started', task=task,
            call_id=getattr(original, 'call_id', None), item_ids=ids, affected_count=len(ids),
            preserved_count=len(rows) - len(ids))
    try:
        repaired = await call('repair_evidence_refs', repair_context, schema, REFERENCE_INSTRUCTION)
    except DomainError as exc:
        exc.item_ids = ids
        raise
    call_id = getattr(repaired, 'call_id', None)
    invalid = next(Draft202012Validator(schema).iter_errors(repaired), None)
    if invalid is not None or len({r['id'] for r in repaired['items']}) != len(ids):
        raise _failure(pending, call_id, '模型的引用修复结果不符合限定字段或条目范围。',
                       retained=save_candidate is not None)
    patches = {row['id']: row for row in repaired['items']}
    unresolved = []
    for row in pending:
        patch = patches[row['id']]
        support = patch['support']
        supported = set(patch['refs']) == {value['ref'] for value in support}
        for value in support:
            text = evidence[value['ref']].get('text', '').strip()
            quote = value['quote'].strip()
            # Prevent meaningless one-character grounding on long passages.
            supported = supported and bool(text) and len(quote) >= min(12, len(text)) and quote in text
        if not patch['refs'] or not patch['reason'].strip() or not supported:
            unresolved.append(row)
            continue
        row['refs'] = copy.deepcopy(patch['refs'])
        _issue(result, row, 'reference_repaired', '已按原文片段修复该条目的证据引用，业务字段保持不变。',
            support=copy.deepcopy(support), reason=patch['reason'])
    if save_candidate:
        save_candidate(result)
    if unresolved:
        raise _failure(unresolved, call_id, retained=save_candidate is not None)
    if diagnostics:
        diagnostics.record('batch.reference_repair_completed', task=task, call_id=call_id, item_ids=ids)
    return result
