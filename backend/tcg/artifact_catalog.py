"""Lightweight saved process outputs; opening this catalog never advances a run."""

PHASE_LABELS = {'understood': '需求理解', 'scenarios_generated': '测试场景',
                'cases_generated': '用例草稿', 'reviewed': '评审结果'}
TYPE_LABELS = {'analysis': '需求理解', 'scenarios': '测试场景', 'cases': '测试用例', 'review': '评审结果'}


def process_artifacts(store, chat_id):
    store.get('chat', chat_id)
    # Only metadata leaves this endpoint. Full items/reports are loaded on selection.
    artifacts = {a['id']: a for a in store.list('artifact', chat_id=chat_id)
                 if a.get('_visible') and a.get('type') in TYPE_LABELS}
    entries = {}

    def add(artifact, revision, phase, created_at, run_id=None):
        if not isinstance(revision, int) or revision < 1 or revision > artifact['revision']:
            return
        key = artifact['id'] + ':' + str(revision)
        entries[key] = {'key': key, 'artifact_id': artifact['id'], 'revision': revision,
            'type': artifact['type'], 'title': artifact['title'], 'phase': phase,
            'label': PHASE_LABELS.get(phase, TYPE_LABELS[artifact['type']]),
            'is_current': revision == artifact['revision'], 'created_at': created_at, 'run_id': run_id}

    for artifact in artifacts.values():
        add(artifact, artifact['revision'], 'saved', artifact.get('updated_at') or artifact.get('created_at', ''))
    for event in store.list('pipeline_result', chat_id=chat_id):
        artifact = artifacts.get(event.get('artifact_id'))
        if artifact:
            add(artifact, event.get('revision'), event.get('phase', 'saved'), event.get('created_at', ''), event.get('run_id'))
    return {'items': sorted(entries.values(), key=lambda entry: (entry['created_at'], entry['key']))}
