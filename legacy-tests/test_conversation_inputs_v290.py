"""Project assumptions must not silently become new task inputs."""
from tcg.documents import parse_text
from tcg.storage import Store
import asyncio


def test_published_message_keeps_original_artifact_revision(tmp_path):
    store = Store(tmp_path)
    try:
        chat = store.create_chat(store.list('project')[0]['id'], 'History')
        text, chunks = parse_text('Original rule may be updated after confirmation.')
        source = store.add_source(chat['id'], 'Requirement', 'primary', text, chunks)
        refs = [source['id'] + '#P1']
        _, run = store.create_run(chat['id'], {'content': 'Analyze', 'intent': 'analyze_requirement', 'mode': 'auto'})
        artifact = store.artifact(run['id'], 'analysis', 'analysis', 'Understanding',
            [{'id': 'R1', 'title': 'Original rule', 'description': 'Original rule', 'refs': refs}])
        store.publish(run['id'], [artifact['id']], waiting=True)
        messages = [m for m in store.list('message', chat_id=chat['id']) if m['role'] == 'assistant']
        store.revise_artifact(artifact['id'], artifact['revision'],
            [{'id': 'R1', 'title': 'Updated rule', 'description': 'Updated rule', 'refs': refs}])
        saved = store.get('message', messages[0]['id'])
        assert saved['metadata']['artifact_revisions'][artifact['id']] == artifact['revision']
        assert store.revision(artifact['id'], saved['metadata']['artifact_revisions'][artifact['id']])['items'] == artifact['items']
    finally:
        store.close()


def test_source_question_uses_only_current_assumptions_and_labels_them(tmp_path):
    from tcg.conversation_artifacts import _analyze_sources
    store = Store(tmp_path)
    try:
        chat = store.create_chat(store.list('project')[0]['id'], 'Scope')
        text, chunks = parse_text('Registered users may log in.')
        base = store.add_source(chat['id'], 'Requirement', 'primary', text, chunks)
        text, chunks = parse_text('Temporarily assume five minutes.')
        old = store.add_source(chat['id'], 'Old assumption', 'clarification', text, chunks)
        store.put('source', {**old, 'status': 'provisional', '_task_only': 'old-run'})
        current = store.add_source(chat['id'], 'Current assumption', 'clarification', text, chunks)
        store.put('source', {**current, 'status': 'provisional'})
        _, run = store.create_run(chat['id'], {'content': 'Use current assumption', 'intent': 'generate_case',
            'mode': 'hitp', 'source_ids': [base['id'], current['id']]})
        store.put('source', {**store.get('source', current['id']), '_task_only': run['id']})

        class Reader:
            def fits(self, *args):
                return True

            async def invoke_model(self, task, context, *args):
                self.context = context
                return {'answer': 'Current task assumption only.', 'refs': []}

        reader = Reader()
        asyncio.run(_analyze_sources(store, reader, chat, {'instruction': 'What assumptions apply?'}))
        sources = {s['id']: s for s in reader.context['sources']}
        assert old['id'] not in sources
        assert sources[current['id']]['status'] == 'provisional'
        assert sources[current['id']]['task_id'] == run['id']
    finally:
        store.close()


def test_shared_fact_metadata_creates_version_and_preserves_evidence_guard(tmp_path):
    import pytest
    from tcg.dependencies import manifest, assert_manifest, DependencyConflict
    from tcg.project_context import share_clarification
    store = Store(tmp_path)
    try:
        chat = store.create_chat(store.list('project')[0]['id'], 'Scope')
        text, chunks = parse_text('Access is revoked immediately.')
        source = store.add_source(chat['id'], 'Revocation', 'clarification', text, chunks)
        before = store.source_snapshot(source['id'])
        shared = share_clarification(store, source['id'], chat['project_id'],
            scope={'module': 'Access'}, fact_key='Revocation time')
        captured = manifest(store, source_ids=[source['id']])
        assert_manifest(store, captured)
        assert shared['version'] > before['source']['version']
        assert store.source_snapshot(source['id'], before['source']['version']) == before
        store.deactivate_source(source['id'])
        with pytest.raises(DependencyConflict):
            assert_manifest(store, captured)
    finally:
        store.close()


def test_new_run_defaults_exclude_prior_provisional_answer(tmp_path):
    store = Store(tmp_path)
    try:
        chat = store.create_chat(store.list('project')[0]['id'], 'Scope')
        text, chunks = parse_text('Registered users may log in.')
        base = store.add_source(chat['id'], 'Requirement', 'primary', text, chunks)
        text, chunks = parse_text('Assume lockout lasts five minutes for the earlier task.')
        assumption = store.add_source(chat['id'], 'Provisional answer', 'clarification', text, chunks)
        store.put('source', {**assumption, 'status': 'provisional', 'scope': {'run_id': 'earlier-run'}})
        _, run = store.create_run(chat['id'], {'content': 'Design tests', 'intent': 'generate_case', 'mode': 'auto'})
        assert base['id'] in run['_source_ids']
        assert assumption['id'] not in run['_source_ids']
    finally:
        store.close()


def test_explicit_assumption_source_remains_available_to_authorized_task(tmp_path):
    store = Store(tmp_path)
    try:
        chat = store.create_chat(store.list('project')[0]['id'], 'Scope')
        text, chunks = parse_text('Temporarily assume lockout lasts five minutes.')
        source = store.add_source(chat['id'], 'Assumption', 'clarification', text, chunks)
        store.put('source', {**source, 'status': 'provisional'})
        _, run = store.create_run(chat['id'], {'content': 'Use this assumption for this task',
            'intent': 'generate_case', 'mode': 'auto', 'source_ids': [source['id']]})
        assert run['_source_ids'] == [source['id']]
    finally:
        store.close()
