"""Read-only access to historical context diagnostics; no orchestration state."""
from .schemas import DomainError
from .storage import public


def list_turn_contexts(store, chat_id, turn_id):
    chat=store.get('chat',chat_id);turn=store.get('conversation_turn',turn_id)
    if turn['chat_id']!=chat_id or turn['project_id']!=chat['project_id']:
        raise DomainError('对话操作不属于当前对话',404)
    return [public(value) for value in sorted(store.list('context_receipt',chat_id=chat_id),key=lambda v:(v.get('created_at',''),v['id']))
            if value.get('turn_id')==turn_id and value.get('project_id')==chat['project_id']]
