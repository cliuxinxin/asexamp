import json

import pytest

from tcg.storage import Store
from tcg.review_proposals import item_changes


def test_item_changes_share_add_delete_and_field_removal_semantics():
    changes = item_changes([{'id': 'A', 'title': 'before', 'removed': None}, {'id': 'B'}],
                           [{'id': 'A', 'title': 'after'}, {'id': 'C'}])
    assert [c['op'] for c in changes] == ['update', 'delete', 'add']
    assert changes[0]['fields'] == ['title', 'removed']


@pytest.mark.parametrize('kind,proposal_type', [('review_proposal', 'review'), ('native_revision_proposal', 'revision')])
def test_old_proposals_convert_once_without_changing_active_prompt(tmp_path, kind, proposal_type):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'existing')
    before = {'id': 'artifact', 'items': [{'id': 'A', 'title': 'before'}]}
    store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', ('artifact', 1, json.dumps(before), 'now', 'create', '[]'))
    proposal = {'id': 'old-proposal', 'artifact_id': 'artifact', 'chat_id': chat['id'], 'project_id': project['id'],
                'items': [{'id': 'A', 'title': 'after'}], 'report': {}, 'base_revision': 1,
                'source_ids': [], 'source_roles': {}, 'dependencies': {}}
    if proposal_type == 'review':
        proposal['artifact_revision'] = proposal.pop('base_revision')
        proposal['run_id'] = 'run'
    store.put(kind, proposal)
    prompt = {'id': 'unchanged-prompt', 'proposal_id': proposal['id'], 'artifact_revision': 1, 'kind': 'artifact_proposal'}
    store.put('chat', {**chat, '_native_artifact_prompt': prompt})
    store.close()
    store = Store(tmp_path)
    converted = store.get('artifact_proposal', proposal['id'])
    assert converted['proposal_type'] == proposal_type
    assert converted['artifact_revision'] == 1 and converted['status'] == 'pending'
    assert converted['changes'] == item_changes(before['items'], proposal['items'])
    assert {'_dependencies', '_source_ids', '_source_roles'} <= converted.keys()
    assert not {'base_revision', 'dependencies', 'source_ids', 'source_roles'} & converted.keys()
    assert store.list(kind) == []
    assert store.get('chat', chat['id'])['_native_artifact_prompt'] == prompt
    store.close()
    store = Store(tmp_path)
    assert store.get('artifact_proposal', proposal['id']) == converted
    store.close()
