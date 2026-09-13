import copy
import json

import pytest

from tcg import dependencies as deps
from tcg.diagnostics import Diagnostics, error_details
from tcg.failure_report import failed_step_report
from tcg.generation_guards import begin_generation, merge_manifests
from tcg.schemas import DomainError
from tcg.storage import Store, now


@pytest.fixture
def domain(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Dependency diagnostics')
    source = store.add_source(chat['id'], 'Requirements', 'primary', 'CONFIDENTIAL BUSINESS RULE',
                              [{'text': 'CONFIDENTIAL BUSINESS RULE', 'location': 'P1'}])
    _, run = store.create_run(chat['id'], {'intent': 'generate_case', 'mode': 'auto', 'content': 'Generate cases'})
    artifact = store.artifact(run['id'], 'scenarios', 'scenarios', 'Scenarios', [
        {'id': 'S1', 'title': 'Scenario', 'description': 'Rule', 'priority': 'P1', 'refs': [source['id'] + '#P1']}])
    yield store, run, source, artifact
    store.close()


def test_operational_metadata_does_not_invalidate_consumed_revision(domain):
    store, run, source, artifact = domain
    before = deps.manifest(store, [artifact['id']], [source['id']], run_id=run['id'])
    store.put('artifact', {**artifact, '_visible': True, '_agent_target': {'id': artifact['id'], 'revision': 1}})
    deps.assert_manifest(store, before)
    assert deps.manifest(store, [artifact['id']], [source['id']], run_id=run['id']) == before


def test_legacy_guard_bridges_only_same_saved_revision(domain):
    store, run, source, artifact = domain
    legacy = deps.manifest(store, [artifact['id']], [source['id']], run_id=run['id'])
    legacy['artifacts'][0].pop('digest_scheme', None)
    legacy['artifacts'][0]['digest'] = deps.digest({k: v for k, v in artifact.items() if k != '_visible'})
    legacy['digest'] = deps.digest({k: v for k, v in legacy.items() if k != 'digest'})
    store.put('artifact', {**artifact, '_visible': True, '_agent_target': {'id': artifact['id'], 'revision': 1}})
    deps.assert_manifest(store, legacy)
    store.update_run(run['id'], _generation_epochs={'cases': [0, None]}, _commit_guards={'cases': legacy})
    fresh = begin_generation(store, run['id'], 'generate_cases', {'dependency_manifest': legacy})
    deps.assert_manifest(store, fresh)
    items = copy.deepcopy(artifact['items'])
    items[0]['title'] = 'Changed business row'
    store.revise_artifact(artifact['id'], 1, items)
    with pytest.raises(DomainError) as conflict:
        deps.assert_manifest(store, legacy)
    assert conflict.value.dependency_changes[0]['reason'] == 'version_changed'


@pytest.mark.parametrize('target', ['source', 'profile', 'artifact', 'report', 'run'])
def test_conflicts_identify_input_without_logging_business_text(domain, target):
    store, run, source, artifact = domain
    value = deps.manifest(store, [artifact['id']], [source['id']], [run['_profile_id']], run['id'])
    if target == 'source':
        store.put('source', {**source, '_text': 'SECRET UPDATED SOURCE'})
    elif target == 'profile':
        profile = store.get('profile', run['_profile_id'])
        store.update_profile(profile['id'], profile['name'], {**profile['config'], 'scope': 'SECRET UPDATED SCOPE'}, profile['version'])
    elif target == 'artifact':
        rows = copy.deepcopy(artifact['items'])
        rows[0]['title'] = 'SECRET UPDATED TITLE'
        store.revise_artifact(artifact['id'], 1, rows)
    elif target == 'report':
        store.annotate_artifact(artifact['id'], 1, {'report': {**artifact.get('report', {}), 'summary': 'SECRET NEW REPORT'}})
    else:
        store.update_run(run['id'], scope='SECRET NEW SCOPE')
    with pytest.raises(DomainError) as conflict:
        deps.assert_manifest(store, value)
    details = error_details(conflict.value)
    change = details['dependency_changes'][0]
    assert change['category'] == ('artifact' if target == 'report' else target)
    assert change['id']
    assert change['expected_digest'] != change['current_digest']
    if target == 'report':
        assert change['changed_fields'] == ['report']
    elif target != 'run':
        assert change['current_version'] > change['expected_version']
    assert 'SECRET' not in json.dumps(details)
    assert 'CONFIDENTIAL' not in json.dumps(details)


def test_later_batch_keeps_earlier_source_guard_and_phase(domain):
    store, run, source, artifact = domain
    context = {'dependency_manifest': deps.manifest(store, [artifact['id']], [source['id']])}
    begin_generation(store, run['id'], 'generate_cases', context)
    store.put('source', {**source, '_text': 'Changed between model batches'})
    with pytest.raises(DomainError) as conflict:
        begin_generation(store, run['id'], 'complete_case_fields', {'evidence': []})
    details = error_details(conflict.value)
    assert details['dependency_phase'] == 'before_generation'
    assert details['dependency_task'] == 'complete_case_fields'
    assert details['dependency_changes'][0]['category'] == 'source'


def test_missing_resource_identifies_exact_reference(domain):
    store, run, source, artifact = domain
    value = deps.manifest(store, [artifact['id']], [source['id']])
    store.db.execute('DELETE FROM objects WHERE id=?', (artifact['id'],))
    with pytest.raises(DomainError) as conflict:
        deps.assert_manifest(store, value)
    assert conflict.value.dependency_changes[0]['id'] == artifact['id']
    assert conflict.value.dependency_changes[0]['reason'] == 'unavailable'


def test_smalllog_includes_commit_conflict_after_successful_json(domain):
    store, run, source, artifact = domain
    diagnostics = Diagnostics(store)
    cid = 'call_valid_json'
    at = now()
    call = {'call_id': cid, 'node': 'cases', 'task': 'generate_cases', 'at': at,
            'messages': [{'role': 'user', 'content': json.dumps({'current_stage': 'generate_cases'})}]}
    store.db.execute('INSERT INTO model_requests VALUES(?,?,?)', (run['id'], cid, json.dumps(call)))
    store.append_model_output(run['id'], cid, '{"items":[],"has_more":false}')
    diagnostics.record('model.complete', run_id=run['id'], node='cases', call_id=cid)
    guard = deps.manifest(store, [artifact['id']], [source['id']])
    store.put('source', {**source, '_text': 'SECRET CHANGED AFTER JSON'})
    with pytest.raises(DomainError) as conflict:
        deps.assert_manifest(store, guard)
    diagnostics.record('node.error', run_id=run['id'], node='cases', **error_details(conflict.value))
    store.update_run(run['id'], status='failed', failed_node='cases', error=str(conflict.value))
    report = failed_step_report(store, diagnostics, run['id']).decode()
    assert 'JSON 语法：通过' in report
    assert 'dependency_changes' in report
    assert source['id'] in report
    assert 'version_changed' in report
    assert 'SECRET CHANGED' not in report


def test_single_call_report_does_not_attribute_later_dependency_failure(domain):
    store, run, source, artifact = domain
    diagnostics = Diagnostics(store)
    for cid in ('call_first', 'call_later'):
        call = {'call_id': cid, 'node': 'cases', 'task': 'generate_cases', 'at': now(), 'messages': []}
        store.db.execute('INSERT INTO model_requests VALUES(?,?,?)', (run['id'], cid, json.dumps(call)))
        store.append_model_output(run['id'], cid, '{"items":[],"has_more":false}')
        diagnostics.record('model.complete', run_id=run['id'], node='cases', call_id=cid)
    guard = deps.manifest(store, [artifact['id']], [source['id']])
    store.put('source', {**source, '_text': 'Changed after later call'})
    with pytest.raises(DomainError) as conflict:
        deps.assert_manifest(store, guard)
    diagnostics.record('node.error', run_id=run['id'], node='cases', **error_details(conflict.value))
    store.update_run(run['id'], status='failed', failed_node='cases', error=str(conflict.value))
    first = failed_step_report(store, diagnostics, run['id'], 'call_first').decode()
    later = failed_step_report(store, diagnostics, run['id'], 'call_later').decode()
    assert 'dependency_changes' not in first
    assert 'call_later' not in first
    assert 'dependency_changes' in later


def test_merge_valid_guards_is_stable_and_same_version_digest_conflict_is_rejected(domain):
    store, run, source, artifact = domain
    value = deps.manifest(store, [artifact['id']], [source['id']], run_id=run['id'])
    assert merge_manifests(value, value, strict=True) == value
    deps.assert_manifest(store, merge_manifests(value, value, strict=True))
    other = copy.deepcopy(value)
    other['artifacts'][0]['digest'] = '0' * 64
    with pytest.raises(DomainError):
        merge_manifests(value, other, strict=True)


def test_multiple_historical_ancestor_versions_keep_current_heads_separate(domain):
    from tcg.context_service import artifact_context
    store, run, source, _ = domain
    analysis = store.artifact(run['id'], 'analysis', 'analysis', 'Requirements', [
        {'id': 'R' + str(i), 'title': 'Rule ' + str(i), 'description': 'Original rule',
         'refs': [source['id'] + '#P1']} for i in (1, 2)])
    source2 = store.put('source', {**source, '_text': 'Revised rules before starting case generation'})
    provenance = deps.manifest(store, source_ids=[source['id']])
    rows = copy.deepcopy(analysis['items'])
    rows[1]['description'] = 'Revised rule two'
    store.revise_artifact(analysis['id'], 1, rows, provenance=provenance)
    parent_manifest = deps.manifest(store,
        artifact_ids=[{'id': analysis['id'], 'revision': v} for v in (1, 2)],
        source_ids=[{'id': source['id'], 'version': v} for v in (1, source2['version'])])
    scene = store.artifact(run['id'], 'mixed_scenes', 'scenarios', 'Mixed ancestry', [
        {'id': 'S' + str(i), 'title': 'Scenario ' + str(i), 'description': 'Scenario', 'priority': 'P1',
         'requirement_ids': ['R' + str(i)], 'refs': [source['id'] + '#P1']} for i in (1, 2)],
        provenance=parent_manifest)
    scene = store.annotate_artifact(scene['id'], scene['revision'], {'report': {
        'lineage': {'analysis_artifact_id': analysis['id'], 'analysis_revision': 2,
                    'analysis_revisions': {'R1': 1, 'R2': 2}}}})
    context = artifact_context(store, 'generate_cases', scene, scene['items'], store.evidence([source['id']]))
    guard = begin_generation(store, run['id'], 'generate_cases', context)
    deps.assert_manifest(store, guard)
    saved = store.run(run['id'])
    assert {a['revision'] for a in saved['_consumed_inputs']['cases']['artifacts'] if a['id'] == analysis['id']} == {1, 2}
    assert {a['revision'] for a in guard['artifacts'] if a['id'] == analysis['id']} == {2}
    assert {s['version'] for s in saved['_consumed_inputs']['cases']['sources']} == {1, 2}
    assert {s['version'] for s in guard['sources']} == {2}
    assert merge_manifests(guard, guard, strict=True) == guard
