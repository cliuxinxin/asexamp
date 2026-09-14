"""One version-bound proposal contract for review and revision of business artifacts."""
import copy

from . import dependencies as deps
from .schemas import DomainError
from .storage import now, public


def item_changes(before, after):
    old, new = {row['id']: row for row in before}, {row['id']: row for row in after}
    result = []
    for item_id in dict.fromkeys([*old, *new]):
        left, right = old.get(item_id), new.get(item_id)
        if left == right:
            continue
        fields = [key for key in dict.fromkeys([*(left or {}), *(right or {})])
                  if (left or {}).get(key) != (right or {}).get(key)
                  or (key in (left or {})) != (key in (right or {}))]
        result.append({'op': 'add' if left is None else 'delete' if right is None else 'update',
                       'id': item_id, 'before': copy.deepcopy(left), 'after': copy.deepcopy(right), 'fields': fields})
    return result


def save_review_proposal(store, run, cases, rows, report, guard, sources, roles, feedback=''):
    identity = deps.digest({'run': run['id'], 'artifact': cases['id'], 'revision': cases['revision'],
                            'items': rows, 'report': report, 'feedback': feedback})
    proposal_id = 'review_' + identity[:32]
    with store.transaction():
        deps.assert_manifest(store, guard)
        if store.run(run['id'])['status'] == 'cancelled':
            raise DomainError('任务已取消', 409)
        try:
            return store.get('artifact_proposal', proposal_id)
        except DomainError as exc:
            if exc.status != 404:
                raise
        return store.put('artifact_proposal', {'id': proposal_id, 'proposal_type': 'review', 'run_id': run['id'],
            'chat_id': run['chat_id'], 'project_id': run['project_id'],
            'artifact_id': cases['id'], 'artifact_revision': cases['revision'],
            'kind': 'cases', 'title': '评审建议与用例修改预览', 'status': 'pending',
            'items': copy.deepcopy(rows), 'report': copy.deepcopy(report),
            'changes': item_changes(cases['items'], rows), 'feedback': feedback,
            '_dependencies': copy.deepcopy(guard), '_source_ids': list(sources),
            '_source_roles': copy.deepcopy(roles), 'created_at': now()})


def read_review_proposal(store, run_id, proposal_id):
    run = store.run(run_id)
    proposal = store.get('artifact_proposal', proposal_id)
    if proposal.get('proposal_type') != 'review':
        raise DomainError('此建议不是任务评审建议', 404)
    if any(proposal.get(key) != run.get(key) for key in ('chat_id', 'project_id')) or proposal['run_id'] != run_id:
        raise DomainError('评审建议不属于当前任务', 404)
    result = public(proposal)
    result['expected_revision'] = proposal['artifact_revision']
    result['before_items'] = store.revision(proposal['artifact_id'], proposal['artifact_revision'])['items']
    result['stale'] = store.get('artifact', proposal['artifact_id'])['revision'] != (
        proposal.get('applied_revision') if proposal['status'] == 'applied' else proposal['artifact_revision'])
    return result


def require_current_review(store, run_id, proposal_id):
    public_proposal = read_review_proposal(store, run_id, proposal_id)
    if public_proposal['stale']:
        raise DomainError('用例已改变，原评审建议已过期；请重新生成评审建议后确认', 409)
    proposal = store.get('artifact_proposal', proposal_id)
    if proposal['status'] not in ('pending', 'applied'):
        raise DomainError('这份评审建议已被更新，请查看当前建议', 409)
    if proposal['status'] == 'pending':
        try:
            deps.assert_manifest(store, proposal['_dependencies'])
        except deps.DependencyConflict as exc:
            raise DomainError('评审建议的依据已更新，请重新生成评审建议后确认', 409) from exc
    return proposal


def revision_proposal(artifact, items, report, dependencies, source_ids, source_roles):
    """Build the common, version-bound proposal payload for all artifact edits."""
    return {'proposal_type': 'revision', 'artifact_id': artifact['id'],
            'artifact_revision': artifact['revision'], 'kind': artifact['type'],
            'chat_id': artifact['chat_id'], 'project_id': artifact['project_id'],
            'run_id': None, 'status': 'pending', 'items': copy.deepcopy(items),
            'report': copy.deepcopy(report), 'changes': item_changes(artifact['items'], items),
            '_dependencies': copy.deepcopy(dependencies), '_source_ids': list(source_ids),
            '_source_roles': copy.deepcopy(source_roles)}


def stage_revision_proposal(store, artifact, proposal, message='修改建议已准备好，请打开成果工作区逐项查看并保存。'):
    """Persist one pending proposal and return the compact workspace entry receipt."""
    from .storage import uid
    value = {**proposal, 'id': uid('revprop_'), 'chat_id': artifact['chat_id'],
        'project_id': artifact['project_id'], 'created_at': now(),
        'changes': item_changes(artifact['items'], proposal['items'])}
    pending = {'id': 'revision:' + value['id'] + ':' + str(artifact['revision']),
        'kind': 'artifact_proposal', 'type': 'artifact_proposal',
        'artifact_id': artifact['id'], 'artifact_revision': artifact['revision'],
        'proposal_id': value['id'], 'title': '查看修改建议', 'message': message}
    with store.transaction():
        deps.assert_manifest(store, value['_dependencies'])
        current = store.get('chat', artifact['chat_id'])
        previous = current.get('_native_artifact_prompt')
        if previous and previous.get('proposal_id'):
            old = store.get('artifact_proposal', previous['proposal_id'])
            if old.get('status') == 'pending':
                store.put('artifact_proposal', {**old, 'status': 'superseded', 'superseded_by': value['id']})
        store.put('artifact_proposal', value)
        store.put('chat', {**current, '_native_artifact_prompt': pending})
    return {'message': message, 'parts': [{'type': 'artifact_proposal', 'artifact_id': artifact['id'],
        'proposal_id': value['id'], 'artifact_revision': artifact['revision']}],
        'status': 'needs_confirmation', 'pending': [pending]}
