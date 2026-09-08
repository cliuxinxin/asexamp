import copy
import json

import pytest

from tcg.incremental_workspace import Workspace, fingerprint, split_evidence
from tcg.schemas import DomainError
from tcg.storage import Store


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def add_source(store, chat_id, source_id, paragraphs, role='primary'):
    return store.add_source(chat_id, source_id, role, '\n\n'.join(paragraphs),
                            [{'text': text, 'location': f'paragraph {i}'}
                             for i, text in enumerate(paragraphs, 1)], source_id=source_id)


@pytest.fixture
def workspace_run(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Incremental work')
    add_source(store, chat['id'], 'src-a', ['Login needs authentication.', 'Retain the cart.'])
    add_source(store, chat['id'], 'src-b', ['Changes retain all text.'], role='change')
    add_source(store, chat['id'], 'src-example', ['Do not infer business facts.'], role='example')
    _, run = store.create_run(chat['id'], {'content': 'Design login tests', 'intent': 'generate_case',
                                        'mode': 'auto', 'experience': 'agent', 'depth': 'deep'})
    run = store.update_run(run['id'], status='running')
    try:
        yield Workspace(store), run
    finally:
        store.close()


def test_fingerprint_is_canonical_and_sensitive_to_all_content():
    assert fingerprint({'b': [1, '界'], 'a': 2}) == fingerprint({'a': 2, 'b': [1, '界']})
    assert fingerprint({'a': 'first'}) != fingerprint({'a': 'last'})
    assert fingerprint([1, 2]) != fingerprint([2, 1])


def test_large_paragraph_parts_reconstruct_original():
    original = {'id': 'src#P1', 'source_id': 'src', 'role': 'primary', 'location': 'Section 2',
                'text': 'BEGIN-' + ('界\\\"\n\x00' * 1800) + '-TAIL'}
    before = copy.deepcopy(original)
    groups = split_evidence([original], budget=1200)
    parts = [e for group in groups for e in group]
    assert len(parts) > 2
    assert ''.join(e['text'] for e in parts) == original['text']
    offset = 0
    for part in parts:
        assert part['excerpt'] == {'start': offset, 'end': offset + len(part['text']),
                                   'total': len(original['text'])}
        assert {key: part[key] for key in ('id', 'source_id', 'role', 'location')} == {
            key: original[key] for key in ('id', 'source_id', 'role', 'location')}
        offset += len(part['text'])
    assert all(size(group) <= 1200 for group in groups)
    assert original == before


def test_groups_preserve_paragraph_boundaries_and_source_order():
    evidence = [{'id': f'{source}#P{i}', 'source_id': source, 'role': 'primary', 'location': str(i), 'text': text}
                for source, i, text in [('a', 1, 'first'), ('a', 2, ''), ('b', 1, 'second'), ('a', 3, 'last')]]
    groups = split_evidence(evidence, budget=1200)
    assert groups == [evidence[:2], evidence[2:3], evidence[3:]]
    groups[0][0]['text'] = 'caller mutation'
    assert evidence[0]['text'] == 'first'
    assert split_evidence([]) == []


@pytest.mark.parametrize('budget', [0, -1, True, 1.5])
def test_invalid_evidence_budgets_are_actionable(budget):
    with pytest.raises(DomainError, match='预算'):
        split_evidence([], budget=budget)


def test_impossible_metadata_budget_fails_explicitly():
    with pytest.raises(DomainError, match='预算'):
        split_evidence([{'id': 'r' * 1000, 'text': 'x'}], budget=100)


def test_units_cover_all_authorized_nonexample_paragraphs_with_stable_ids(workspace_run):
    ws, run = workspace_run
    add_source(ws.store, run['chat_id'], 'src-unselected', ['PRIVATE OUTSIDE SCOPE'])
    units = ws.units(run, budget=1200)
    assert [u['source_ids'] for u in units] == [['src-a'], ['src-b']]
    assert [e['id'] for u in units for e in u['evidence']] == ['src-a#P1', 'src-a#P2', 'src-b#P1']
    assert all(u['refs'] == [e['id'] for e in u['evidence']] for u in units)
    assert all(size(u['evidence']) <= 1200 for u in units)
    assert ws.units(run, budget=1200) == units
    assert 'PRIVATE OUTSIDE SCOPE' not in json.dumps(units)
    another_chat = ws.store.create_chat(run['project_id'], 'Same sources in another run')
    _, second_run = ws.store.create_run(another_chat['id'], {'content': 'Other instruction', 'intent': 'generate_case',
                                                           'mode': 'auto', 'source_ids': run['_source_ids']})
    assert [u['id'] for u in ws.units(second_run, budget=1200)] == [u['id'] for u in units]


def test_units_read_only_the_selected_source_and_preserve_large_tail(workspace_run, monkeypatch):
    ws, run = workspace_run
    giant = 'START-' + 'all-business-evidence-' * 1200 + '-TAIL'
    add_source(ws.store, run['chat_id'], 'src-large', [giant])
    run = ws.store.update_run(run['id'], _source_ids=['src-large'])
    calls = []
    original = ws.store.evidence

    def traced(source_ids):
        calls.append(source_ids)
        return original(source_ids)

    monkeypatch.setattr(ws.store, 'evidence', traced)
    units = ws.units(run, budget=1200)
    assert ''.join(e['text'] for u in units for e in u['evidence']) == giant
    assert all(u['refs'] == ['src-large#P1'] for u in units)
    assert all(u['source_ids'] == ['src-large'] for u in units)
    assert len({u['id'] for u in units}) == len(units)
    assert calls == [['src-large']]


def test_units_reject_stale_or_foreign_scope(workspace_run):
    ws, run = workspace_run
    other_project = ws.store.create_project('Other')
    other_chat = ws.store.create_chat(other_project['id'], 'Other')
    add_source(ws.store, other_chat['id'], 'src-foreign', ['SECRET'])
    with pytest.raises(DomainError):
        ws.units({**run, '_source_ids': ['src-foreign']})
    ws.store.update_run(run['id'], _source_ids=['src-b'])
    with pytest.raises(DomainError):
        ws.units(run)


def test_deactivated_sources_are_not_read(workspace_run):
    ws, run = workspace_run
    ws.store.deactivate_source('src-a')
    units = ws.units(run)
    assert [u['source_ids'] for u in units] == [['src-b']]


def test_accept_is_durable_and_idempotent(workspace_run, tmp_path):
    ws, run = workspace_run
    ws.begin(run['id'], 'analysis:one', 'analysis', 'Login', refs=['src-a#P1'])
    accepted = ws.accept(run['id'], 'analysis:one', {'items': ['kept']}, artifact_id='art-kept')
    event_count = len(ws.store.events(run['id']))
    reopened = Store(tmp_path)
    try:
        resumed = Workspace(reopened)
        assert resumed.begin(run['id'], 'analysis:one', 'different', 'Replacement') == accepted
        assert resumed.accept(run['id'], 'analysis:one', {'items': ['lost']}, artifact_id='art-lost') == accepted
        assert resumed.get(run['id'], 'analysis:one')['_result'] == {'items': ['kept']}
        assert len(reopened.events(run['id'])) == event_count
    finally:
        reopened.close()


def test_work_keys_are_run_scoped(workspace_run):
    ws, run = workspace_run
    chat = ws.store.create_chat(run['project_id'], 'Parallel')
    _, other = ws.store.create_run(chat['id'], {'content': 'Other', 'intent': 'query', 'mode': 'auto'})
    one = ws.begin(run['id'], 'shared:key', 'query', 'One')
    two = ws.begin(other['id'], 'shared:key', 'query', 'Two')
    ws.accept(run['id'], 'shared:key', {'secret': 'only this run'})
    assert one['id'] != two['id']
    assert ws.get(other['id'], 'shared:key')['status'] == 'running'
    assert ws.get(other['id'], 'missing') is None
    assert [item['title'] for item in ws.list(other['id'])] == ['Two']


def test_failed_and_interrupted_work_retries_without_losing_dependencies(workspace_run):
    ws, run = workspace_run
    first = ws.begin(run['id'], 'case:one', 'cases', 'Login', ['src-a#P1'], ['analysis:one'])
    assert first['attempt'] == 1
    failed = ws.fail(run['id'], 'case:one', 'Validation rejected the response')
    assert failed['status'] == 'failed'
    retry = ws.begin(run['id'], 'case:one', 'cases', 'Login', ['src-a#P1'], ['analysis:one'])
    assert retry['attempt'] == 2
    assert retry['error'] is None
    assert retry['dependencies'] == ['analysis:one']
    assert ws.begin(run['id'], 'case:one', 'cases', 'Login')['attempt'] == 3
    ws.accept(run['id'], 'case:one', {'value': 'accepted'})
    assert ws.fail(run['id'], 'case:one', 'late failure')['_result'] == {'value': 'accepted'}


@pytest.mark.parametrize('status', ['cancelled', 'completed', 'failed', 'waiting'])
@pytest.mark.parametrize('operation', ['begin', 'accept', 'fail'])
def test_all_mutations_reject_inactive_runs_without_events(workspace_run, status, operation):
    ws, run = workspace_run
    ws.begin(run['id'], 'one', 'analysis', 'One')
    ws.store.update_run(run['id'], status=status)
    before = ws.get(run['id'], 'one')
    events = ws.store.events(run['id'])
    with pytest.raises(DomainError) as error:
        if operation == 'begin':
            ws.begin(run['id'], 'one', 'analysis', 'Changed')
        elif operation == 'accept':
            ws.accept(run['id'], 'one', {'private': 'body'})
        else:
            ws.fail(run['id'], 'one', 'late error')
    assert error.value.status == 409
    assert ws.get(run['id'], 'one') == before
    assert ws.store.events(run['id']) == events


def test_public_view_and_events_never_expose_raw_results(workspace_run):
    ws, run = workspace_run
    ws.begin(run['id'], 'one', 'analysis', 'One')
    ws.accept(run['id'], 'one', {'body': 'PRIVATE RAW RESULT', '_result': {'nested': 'private'}})
    ws.begin(run['id'], 'two', 'cases', 'Two')
    view = ws.view(run['id'])
    assert view['completed'] == 1 and view['total'] == 2
    assert view['current']['key'] == 'two'
    assert view['items'] == ws.list(run['id'])
    allowed = {'id', 'key', 'kind', 'title', 'status', 'refs', 'artifact_id', 'attempt', 'error'}
    assert all(set(item) == allowed for item in view['items'])
    assert 'PRIVATE RAW RESULT' not in json.dumps(view)
    events = [event for event in ws.store.events(run['id']) if event['kind'] == 'agent_work']
    assert len(events) == 3
    assert events[-1]['data'] == view
    assert 'PRIVATE RAW RESULT' not in json.dumps(events)


def test_work_mutation_rolls_back_when_event_cannot_be_written(workspace_run):
    ws, run = workspace_run
    ws.begin(run['id'], 'one', 'analysis', 'One')
    ws.store.db.execute("CREATE TRIGGER fail_work_events BEFORE INSERT ON events WHEN NEW.kind = 'agent_work' "
                        "BEGIN SELECT RAISE(ABORT, 'event storage failed'); END")
    before = ws.get(run['id'], 'one')
    with pytest.raises(Exception, match='event storage failed'):
        ws.accept(run['id'], 'one', {'value': 'must rollback'})
    assert ws.get(run['id'], 'one') == before


@pytest.mark.parametrize(('task', 'expected'), [
    ('work_analyze', {'scope', 'language', 'additional_rules'}),
    ('work_scenarios', {'scenario_schema', 'scenario_level', 'additional_rules'}),
    ('work_cases', {'case_schema', 'case_level', 'case_types', 'template', 'additional_rules'}),
    ('work_review', {'review_rules', 'review_dimensions', 'review_schema'}),
    ('work_query', set()),
])
def test_model_context_uses_task_specific_profile_and_supplied_business_values(workspace_run, task, expected):
    ws, run = workspace_run
    profile = {key: f'value for {key}' for key in {
        'scope', 'language', 'additional_rules', 'scenario_schema', 'scenario_level', 'case_schema',
        'case_level', 'case_types', 'template', 'review_rules', 'review_dimensions', 'review_schema', 'secret_config'}}
    run['_profile'] = profile
    run['_conversation'] = [{'content': 'PRIVATE HISTORY'}]
    run['_artifact_snapshot'] = {'body': 'PRIVATE FULL ARTIFACT'}
    supplied = {'items': [{'id': 'REQ-1', 'description': 'Business value must remain complete'}],
                'business_model': {'arbitrary_domain_property': 'MUST KEEP'},
                'scope_decision': {'why': 'required caller value'}}
    context = ws.model_context(run, task, supplied)
    assert context['profile'] == {key: profile[key] for key in expected}
    assert context['goal'] == 'Design login tests'
    assert context['depth'] == 'deep'
    assert all(context[key] == value for key, value in supplied.items())
    assert 'PRIVATE HISTORY' not in json.dumps(context)
    assert 'PRIVATE FULL ARTIFACT' not in json.dumps(context)
    assert 'secret_config' not in context['profile']
    context['items'][0]['description'] = 'caller mutation'
    assert supplied['items'][0]['description'] == 'Business value must remain complete'


def test_goal_does_not_duplicate_requirement_source_and_explicit_override_is_preserved(workspace_run):
    ws, run = workspace_run
    original = ws.store.get('source', 'src-a')['_text']
    run['_request']['content'] = original
    context = ws.model_context(run, 'work_analyze', {})
    assert original not in context['goal']
    assert run['intent'] in context['goal']
    run['_request']['as_requirement'] = True
    run['_request']['content'] = 'INLINE DOCUMENT BODY ' * 2000
    assert 'INLINE DOCUMENT BODY' not in ws.model_context(run, 'work_analyze', {})['goal']
    assert ws.model_context(run, 'work_analyze', {'goal': 'Read this specific unit'})['goal'] == 'Read this specific unit'


@pytest.mark.parametrize('large_field', ['data', 'rules', 'goal'])
def test_oversize_context_rejects_instead_of_truncating(workspace_run, large_field):
    ws, run = workspace_run
    data = {'evidence': [{'text': 'keep this intact'}]}
    if large_field == 'data':
        data['business_value'] = 'SECRET-SENTINEL' * 300
    elif large_field == 'rules':
        run['_profile']['additional_rules'] = 'SECRET-SENTINEL' * 300
    else:
        data['goal'] = 'SECRET-SENTINEL' * 300
    before = copy.deepcopy(data)
    with pytest.raises(DomainError) as error:
        ws.model_context(run, 'work_analyze', data, budget=1200)
    assert '预算' in error.value.message and '1200' in error.value.message
    assert 'SECRET-SENTINEL' not in error.value.message
    assert data == before


def test_unused_large_rules_do_not_block_query_context(workspace_run):
    ws, run = workspace_run
    run['_profile']['additional_rules'] = 'unrelated rules' * 10000
    context = ws.model_context(run, 'work_query', {'question': 'Where is login?'}, budget=300)
    assert size(context) <= 300
    assert context['question'] == 'Where is login?'
    assert context['profile'] == {}


def test_context_budget_counts_the_actual_model_json_serialization(workspace_run):
    ws, run = workspace_run
    data = {'values': [0] * 200}
    # Compact JSON would fit; the actual ModelClient inserts separating spaces.
    with pytest.raises(DomainError, match='预算'):
        ws.model_context(run, 'work_query', data, budget=550)


def test_resplitting_a_part_keeps_offsets_in_the_original_paragraph():
    text = 'MIDDLE-' + 'full source offsets ' * 200
    evidence = {'id': 'src#P1', 'source_id': 'src', 'text': text,
                'excerpt': {'start': 500, 'end': 500 + len(text), 'total': 10000}}
    groups = split_evidence([evidence], budget=1000)
    parts = [item for group in groups for item in group]
    assert ''.join(item['text'] for item in parts) == text
    offset = 500
    for part in parts:
        assert part['excerpt'] == {'start': offset, 'end': offset + len(part['text']), 'total': 10000}
        offset += len(part['text'])
