"""Server rejections govern project context splitting; writes remain atomic."""
import copy

import pytest

from tcg.conversation_artifacts import execute as artifact_execute
from tcg.conversation_project import execute
from tcg.server_capacity import ContextCapacityError
from test_conversation_artifacts_v260 import data
from test_conversation_project_v260 import fixture, source, dual_template
from test_framework_impact_v270 import setup


class Server:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def fits(self, *_):
        raise AssertionError('Local estimates must not gate server requests')

    async def invoke_model(self, task, context, run_id=None):
        self.calls.append((task, copy.deepcopy(context)))
        return self.respond(task, context)


@pytest.mark.asyncio
async def test_source_impact_reactively_covers_full_chunk_product(tmp_path):
    store, chat, analysis, new, _ = setup(tmp_path, requirement_count=3, chunk_count=3)
    evidence = store.evidence([new['id']])
    before = store.revisions(analysis['id'])
    accepted = []
    def respond(task, context):
        assert task == 'project_source_impact'
        rows, chunks = context['requirements'], context['new_evidence']
        if len(rows) > 1 or len(chunks) > 1:
            raise ContextCapacityError(context_limit_tokens=4096)
        accepted.append(copy.deepcopy(context))
        return {'requirement_ids': [rows[0]['id']], 'summary': 'Rule requires confirmation',
            'refs': [chunks[0]['id']], 'uncertain': False, 'global_impact': False}
    server = Server(respond)
    try:
        result = await execute(store, server, chat, 'project.source_impact',
            {'artifact_id': analysis['id'], 'source_ids': [new['id']]})
        first = server.calls[0][1]
        assert len(first['requirements']) == len(first['new_evidence']) == 3
        pairs = {(row['id'], chunk['id']) for ctx in accepted
            for row in ctx['requirements'] for chunk in ctx['new_evidence']}
        assert pairs == {(r['id'], e['id']) for r in analysis['items'] for e in evidence}
        original = {e['id']: e for e in first['new_evidence']}
        assert all(chunk == original[chunk['id']] for ctx in accepted for chunk in ctx['new_evidence'])
        coverage = result['parts'][0]['data']['coverage']
        assert coverage['checked_pairs'] == coverage['expected_pairs'] == 9
        assert coverage['partial'] is False
        assert store.revisions(analysis['id']) == before
    finally:
        store.close()


@pytest.mark.asyncio
async def test_oversized_second_template_never_publishes_partial_config():
    with fixture() as (store, chat, profile):
        first = source(store, chat, 'First template', 'example', 'title,steps,expected')
        second = source(store, chat, 'Large template', 'example', 'Fields ' * 80000)
        def respond(task, context):
            assert task == 'learn_template'
            if context['format_references'][0]['source_id'] == second['id']:
                assert len(str(context)) > 490000
                raise ContextCapacityError()
            return dual_template()
        server = Server(respond)
        with pytest.raises(ContextCapacityError, match='完整工作表'):
            await execute(store, server, chat, 'project.learn_template',
                {'source_ids': [first['id'], second['id']], 'apply': True})
        assert len(server.calls) == 2
        assert store.get('profile', profile['id']) == profile
        assert store.list('template', chat_id=chat['id']) == []


@pytest.mark.asyncio
async def test_large_cross_version_compare_reaches_server_then_stops_safely(tmp_path):
    fixture_data = data(tmp_path)
    store, chat, _, scenarios, _, _, _ = next(fixture_data)
    items = copy.deepcopy(scenarios['items'])
    items[0]['description'] = 'Scenario details ' * 40000
    scenarios = store.revise_artifact(scenarios['id'], scenarios['revision'], items)
    def respond(task, context):
        assert task == 'artifact_compare'
        assert len(str(context)) > 490000
        raise ContextCapacityError()
    server = Server(respond)
    try:
        with pytest.raises(ContextCapacityError, match='跨版本比较'):
            await artifact_execute(store, server, chat, 'artifact.analyze',
                {'artifact_id': scenarios['id'], 'artifact_ids': [scenarios['id'], 'cases'],
                 'instruction': 'Compare the saved scenario and case snapshots'})
        assert len(server.calls) == 1
        assert store.get('artifact', scenarios['id']) == scenarios
    finally:
        next(fixture_data, None)
