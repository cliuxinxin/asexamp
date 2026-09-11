"""Small persisted command receipts, committed with the domain mutation."""
import copy
from .storage import now
from .schemas import DomainError

FINAL = {'succeeded', 'cancelled'}

def commit_result(store, command_id, result):
    if not command_id:
        return result
    command = store.get('conversation_command', command_id)
    if command['status'] == 'cancelled':
        raise DomainError('这项对话操作已取消，未保存晚到结果', 409)
    if command['status'] == 'succeeded':
        return copy.deepcopy(command['result'])
    command.update(status=result.get('status', 'succeeded'), result=copy.deepcopy(result), updated_at=now())
    store.put('conversation_command', command)
    return result


def assert_command_live(store, command_id):
    if command_id and store.get('conversation_command', command_id)['status'] == 'cancelled':
        raise DomainError('这项对话操作已取消', 409)
