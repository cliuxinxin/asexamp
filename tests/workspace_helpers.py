"""Exercise the actual workspace save route in chat/pipeline integration tests."""

def save_workspace(client, prompt, *, request_id='workspace-save', reject=False):
    artifact_id = prompt['artifact_id']
    query = {'proposal_id': prompt['proposal_id']}
    if prompt.get('run_id'):
        query['run_id'] = prompt['run_id']
    response = client.get('/api/artifacts/' + artifact_id + '/workspace-grid', params=query)
    assert response.status_code == 200, response.text
    grid = response.json()
    body = {'expected_revision': grid['artifact_revision'], 'proposal_id': prompt['proposal_id'],
        'prompt_id': prompt['id'], 'items': grid['original_items'] if reject else grid['proposed_items'],
        'layout': grid['layout'], 'client_request_id': request_id}
    for field in ('run_id', 'profile_id', 'profile_revision'):
        if grid.get(field) is not None:
            body[field] = grid[field]
    response = client.post('/api/artifacts/' + artifact_id + '/workspace-grid/save', json=body)
    assert response.status_code == 200, response.text
    return response.json(), body
