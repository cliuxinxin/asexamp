"""Read-only projection of the current, version-bound artifact edit proposal."""
import copy

from .schemas import DomainError


def pending_artifact_preview(store, chat):
    pending = chat.get('_native_artifact_prompt')
    if not pending:
        return None
    try:
        proposal = store.get('artifact_proposal', pending['proposal_id'])
        artifact = store.get('artifact', proposal['artifact_id'])
    except (DomainError, KeyError):
        return None
    if (proposal.get('status') != 'pending' or
        proposal.get('chat_id') != chat['id'] or proposal.get('project_id') != chat['project_id'] or
        artifact.get('chat_id') != chat['id'] or artifact.get('project_id') != chat['project_id'] or
        artifact['revision'] != proposal['artifact_revision'] or
        pending.get('artifact_revision') != artifact['revision']):
        return None
    return {'id': proposal['id'], 'prompt_id': pending['id'], 'artifact_id': artifact['id'],
        'summary': pending.get('message', '请查看本次修改。'),
        'changes': copy.deepcopy(proposal['changes'])}
