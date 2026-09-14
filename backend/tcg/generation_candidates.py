"""Read-only failed generation outputs, separate from validated artifacts and lineage."""
import copy

from .schemas import DomainError
from .storage import now, public, uid

STAGE_LABELS = {'understand': '需求理解', 'understand_requirements': '需求理解',
    'apply_clarification': '更新需求理解', 'scenarios': '生成场景', 'generate_scenarios': '生成场景',
    'cases': '生成用例', 'generate_cases': '生成用例', 'review': '评审用例', 'review_cases': '评审用例'}
TASK_KINDS = {'understand_requirements': 'analysis', 'generate_scenarios': 'scenarios',
              'generate_cases': 'cases', 'review_cases': 'cases'}


def save_candidate(store, run, exc):
    """Keep all rejected attempts; never create a normal artifact or successful cache entry."""
    history = copy.deepcopy(getattr(exc, 'candidate_history', None))
    if not history:
        return None
    # A refs-only tool submission is not a complete business item. The repair
    # owner retains the latest full candidate while archiving patches separately.
    selected = history[-1]
    raw = copy.deepcopy(getattr(exc, 'candidate', None))
    if raw is None:
        business = [entry for entry in history if entry.get('task') == getattr(exc, 'task', '')]
        raw = copy.deepcopy((business or history)[-1].get('result'))
    detail = copy.deepcopy(getattr(exc, 'details', {}) or selected.get('details') or {})
    missing = detail.get('missing_input_ids', getattr(exc, 'missing_ids', []))
    issue = {'message': selected.get('validation_error') or str(exc),
        'category': detail.get('category') or getattr(exc, 'category', 'validation'),
        'missing_input_ids': missing, 'call_id': selected.get('call_id') or getattr(exc, 'call_id', None)}
    stage = run.get('stage') or getattr(exc, 'task', '')
    record = {'id': uid('candidate_'), 'run_id': run['id'], 'chat_id': run['chat_id'],
        'project_id': run['project_id'], 'created_at': now(), 'stage': stage,
        'kind': TASK_KINDS.get(getattr(exc, 'task', ''), 'unknown'),
        'title': STAGE_LABELS.get(stage, '本步骤') + ' · 待核对草稿',
        'items': raw.get('items', []) if isinstance(raw, dict) and isinstance(raw.get('items'), list) else [],
        'report': raw.get('report', {}) if isinstance(raw, dict) else {},
        'issues': [issue], 'attempts': getattr(exc, 'retry_count', 3),
        'max_retries': 3, 'request_count': getattr(exc, 'attempts', len(history)),
        'selected_attempt': selected.get('attempt'), 'raw_result': raw, 'history': history,
        'completed_batches': copy.deepcopy(getattr(exc, 'completed_batches', [])),
        'validation_status': 'needs_attention'}
    store.put('generation_candidate', record)
    return {'type': 'generation_candidate', 'candidate_id': record['id'], 'run_id': run['id'],
        **{key: record[key] for key in ('title', 'stage', 'attempts', 'max_retries', 'request_count', 'issues')},
        'item_count': len(record['items'])}


def read_candidate(store, run_id, candidate_id):
    run = store.run(run_id)
    record = store.get('generation_candidate', candidate_id)
    if (record['run_id'] != run_id or record['chat_id'] != run['chat_id'] or
            record['project_id'] != run['project_id']):
        raise DomainError('未找到本任务的生成草稿', 404)
    return public(record)
