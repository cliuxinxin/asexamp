"""Version binding shared by graph gates and every artifact commit path."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar

from .schemas import DomainError


_owner = ContextVar('tcg_confirmation_command', default=None)


@contextmanager
def command_owner(store, command_id):
    """Carry the owning command through async work without a shared global lock."""
    token = _owner.set((store, command_id))
    try:
        yield
    finally:
        _owner.reset(token)


def run_binding(run):
    return {'id': run['id'], 'status': run['status'], 'interrupt_id': run.get('_interrupt_id'),
            'control_version': run.get('control_version', 0),
            'artifact_revision': (run.get('interrupt') or {}).get('artifact_revision')}


def _record_transition(store, run, before):
    owner = _owner.get()
    if not owner or owner[0] is not store or not owner[1]:
        return
    command = store.get('conversation_command', owner[1])
    if command.get('chat_id') != run['chat_id'] or command.get('project_id') != run['project_id']:
        raise DomainError('修改命令不属于待确认任务', 403)
    if command.get('status') == 'cancelled':
        raise DomainError('修改命令已取消，未保存结果', 409)
    change = {'before': before, 'after': run_binding(run)}
    changes = command.setdefault('_run_binding_changes', [])
    if change not in changes:
        changes.append(change)
    store.put('conversation_command', command)


def bind_interrupt(store, value):
    result = copy.deepcopy(value)
    if result.get('artifact_id'):
        artifact = store.get('artifact', result['artifact_id'])
        result['artifact_revision'] = artifact['revision']
    return result


def refresh_confirmation(store, artifact):
    """Called inside the artifact transaction; does not resume the graph."""
    for run in store.runs(chat_id=artifact['chat_id'], statuses=('waiting',)):
        pending = run.get('interrupt') or {}
        if pending.get('artifact_id') != artifact['id']:
            continue
        if pending.get('artifact_revision') == artifact['revision']:
            continue
        before = run_binding(run)
        run['interrupt'] = {**pending, 'artifact_revision': artifact['revision']}
        if 'items' in pending:
            run['interrupt']['items'] = copy.deepcopy(artifact['items'])
        version = run.get('control_version', 0) + 1
        run.update(control_version=version, _control_version=version)
        store.save_run(run)
        _record_transition(store, run, before)


def validate_confirmation(store, run, response):
    """Old direct clients may omit bindings; supplied tokens are always checked."""
    if response.get('interrupt_id') is not None and response['interrupt_id'] != run.get('_interrupt_id'):
        raise DomainError('确认节点已改变，请刷新当前结果后确认', 409)
    if response.get('expected_control_version') is not None and response['expected_control_version'] != run.get('control_version', 0):
        raise DomainError('任务或待确认成果已更新，请查看新版本后确认', 409)
    pending = run.get('interrupt') or {}
    if response.get('expected_revision') is not None:
        if not pending.get('artifact_id'):
            raise DomainError('此确认节点没有可确认的成果版本', 409)
        current = store.get('artifact', pending['artifact_id'])
        if response['expected_revision'] != current['revision']:
            raise DomainError('待确认成果已更新，请查看新版本后确认', 409)
