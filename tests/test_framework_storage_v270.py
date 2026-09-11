import copy
import json
import sqlite3

import pytest

from tcg.storage import Store
from tcg.schemas import DomainError
from tcg import dependencies
from tcg.operations import cancel_command


@pytest.fixture
def domain(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], '版本测试')
    source = store.add_source(chat['id'], '需求', 'requirement', '两条规则', [{'text': '两条规则', 'location': 'P1'}])
    _, run = store.create_run(chat['id'], {'intent': 'auto', 'mode': 'auto', 'content': '生成'})
    scenes = store.artifact(run['id'], 'scenes', 'scenarios', '场景', [
        {'id': f'S{i}', 'title': f'场景{i}', 'description': f'规则{i}', 'priority': 'P1', 'refs': [source['id'] + '#P1']}
        for i in (1, 2)])
    store.cache_set(run['id'], 'scenarios_artifact', {'id': scenes['id'], 'revision': 1})
    cases = store.artifact(run['id'], 'cases', 'cases', '用例', [
        {'id': f'C{i}', 'title': f'用例{i}', 'scenario_id': f'S{i}', 'type': 'Business', 'priority': 'P1',
         'preconditions': '', 'steps': [{'action': '验证', 'expected': f'规则{i}'}], 'refs': [source['id'] + '#P1']}
        for i in (1, 2)])
    yield store, run, source, scenes, cases
    store.close()


def test_report_annotation_preserves_history(domain):
    store, run, source, scenes, cases = domain
    historical = store.revision(scenes['id'], 1)
    revised = store.annotate_artifact(scenes['id'], 1, {'report': {'summary': '已复核'}})
    assert revised['revision'] == 2
    assert store.get('artifact', scenes['id']) == revised
    assert store.revision(scenes['id'], 1) == historical
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute('UPDATE revisions SET payload=? WHERE artifact_id=? AND revision=1', ('{}', scenes['id']))


def test_manifest_tracks_source_scope_profile_and_not_stop_policy(domain):
    store, run, source, scenes, cases = domain
    value = dependencies.manifest(store, [scenes['id']], [source['id']], [run['_profile_id']], run['id'])
    dependencies.assert_manifest(store, value)
    changed_run = store.run(run['id'])
    changed_run['_request']['stop_after'] = 'analysis'
    changed_run['control_version'] = 2
    store.save_run(changed_run)
    dependencies.assert_manifest(store, value)
    changed_run['_profile']['scope'] = '只验证第二场景'
    store.save_run(changed_run)
    with pytest.raises(DomainError):
        dependencies.assert_manifest(store, value)
    fresh = dependencies.manifest(store, source_ids=[source['id']])
    original = store.get('source', source['id'])
    changed = store.put('source', {**original, '_text': '不同规则'})
    assert changed['version'] == original['version'] + 1
    assert changed['_content_digest'] != original['_content_digest']
    with pytest.raises(DomainError):
        dependencies.assert_manifest(store, fresh)
    snapshot = store.db.execute('SELECT payload FROM input_versions WHERE object_id=? AND kind=? AND version=?',
                                (source['id'], 'source', original['version'])).fetchone()
    assert json.loads(snapshot[0])['_text'] == original['_text']


def test_relations_precise_scope_and_partial_sync(domain):
    store, run, source, scenes, cases = domain
    assert dependencies.artifact_status(store, cases)['status'] == 'current'
    items = copy.deepcopy(scenes['items'])
    items[0]['description'] = '更新规则1'
    updated = store.revise_artifact(scenes['id'], 1, items)
    status = dependencies.artifact_status(store, cases)
    assert status['status'] == 'stale'
    assert status['affected_item_ids'] == ['C1']
    impact = dependencies.impact(store, scenes['id'], ['S2'])
    assert impact['affected'][0]['affected_item_ids'] == ['C2']
    assert store.revision(cases['id'], 1) == cases
    report = copy.deepcopy(cases['report'])
    report['lineage']['scenario_revisions'] = {'S1': updated['revision']}
    synced = store.revise_artifact(cases['id'], 1, cases['items'], report=report)
    assert dependencies.artifact_status(store, synced)['status'] == 'current'


def test_receipts_replay_exact_version_after_later_edits(domain):
    store, run, source, scenes, cases = domain
    first = store.revise_artifact(scenes['id'], 1, scenes['items'], command_id='edit-one')
    store.revise_artifact(scenes['id'], 2, scenes['items'])
    assert store.revise_artifact(scenes['id'], 1, [], command_id='edit-one') == first
    assert store.artifact(run['id'], 'scenes', 'scenarios', '重试', []) == scenes
    with pytest.raises(DomainError):
        store.revise_artifact(scenes['id'], 1, scenes['items'])
    cancel_command(store, 'cancelled')
    with pytest.raises(DomainError):
        store.revise_artifact(scenes['id'], 3, scenes['items'], command_id='cancelled')
    assert store.get('artifact', scenes['id'])['revision'] == 3


def test_enrichment_and_stale_dependencies_are_atomic(domain):
    store, run, source, scenes, cases = domain
    supplement = store.add_source(run['chat_id'], '补充', 'requirement', '新增规则', [{'text': '新增规则', 'location': 'P1'}])
    items = copy.deepcopy(scenes['items'])
    items[0]['refs'].append(supplement['id'] + '#P1')
    consumed = dependencies.manifest(store, source_ids=[source['id']])
    store.put('source', {**store.get('source', source['id']), 'role': 'change'})
    with pytest.raises(DomainError):
        store.revise_artifact(scenes['id'], 1, items, source_ids=[supplement['id']], dependencies=consumed)
    assert store.get('artifact', scenes['id']) == scenes
    revised = store.revise_artifact(scenes['id'], 1, items, source_ids=[supplement['id']], source_roles={supplement['id']: 'requirement'})
    assert supplement['id'] in revised['_source_ids']
    assert supplement['id'] not in store.revision(scenes['id'], 1)['_source_ids']


def test_current_business_fields_and_head_cannot_be_rewritten(domain):
    store, run, source, scenes, cases = domain
    with pytest.raises(DomainError):
        store.put('artifact', {**scenes, 'report': {'summary': '原地修改'}})
    published = store.put('artifact', {**scenes, '_visible': True})
    assert published['_visible'] is True
    assert store.revision(scenes['id'], 1)['_visible'] is False
    store.revise_artifact(scenes['id'], 1, scenes['items'])
    with pytest.raises(DomainError):
        store.put('artifact', scenes)


def test_consumed_self_baseline_is_not_a_persisted_dependency(domain):
    store, run, source, scenes, cases = domain
    consumed = dependencies.manifest(store, artifact_ids=[scenes['id']], source_ids=[source['id']])
    revised = store.revise_artifact(scenes['id'], 1, scenes['items'], dependencies=consumed)
    assert revised['_dependencies']['artifacts'] == []
    assert dependencies.artifact_status(store, revised)['status'] == 'current'
    historical = dependencies.manifest(store, artifact_ids=[{'id': scenes['id'], 'revision': 1}, {'id': scenes['id'], 'revision': 2}])
    assert [r['revision'] for r in historical['artifacts']] == [1, 2]
    with pytest.raises(DomainError):
        dependencies.assert_manifest(store, historical)


def test_profile_snapshots_and_cancelled_run_commit(domain):
    store, run, source, scenes, cases = domain
    profile = store.get('profile', run['_profile_id'])
    consumed = dependencies.manifest(store, profile_ids=[profile['id']])
    store.update_profile(profile['id'], '新配置', {**profile['config'], 'scope': '仅场景1'}, profile['version'])
    with pytest.raises(DomainError):
        dependencies.assert_manifest(store, consumed)
    row = store.db.execute('SELECT payload FROM input_versions WHERE object_id=? AND kind=? AND version=1', (profile['id'], 'profile')).fetchone()
    assert json.loads(row[0]) == profile
    store.update_run(run['id'], status='cancelled')
    with pytest.raises(DomainError):
        store.revise_artifact(scenes['id'], 1, scenes['items'], run_id=run['id'])
    assert store.artifact(run['id'], 'scenes', 'scenarios', '重试', []) == scenes


def test_direct_head_advance_requires_saved_revision(domain):
    store, run, source, scenes, cases = domain
    with pytest.raises(DomainError):
        store.put('artifact', {**scenes, 'revision': 2, 'title': '绕过提交'})
    assert store.get('artifact', scenes['id']) == scenes
    assert len(store.revisions(scenes['id'])) == 1


def test_annotation_and_ordinary_edit_preserve_stale_provenance(domain):
    store, run, source, scenes, cases = domain
    original = scenes['_dependencies']
    store.put('source', {**store.get('source', source['id']), '_text': '更新后的需求'})
    assert dependencies.artifact_status(store, scenes)['status'] == 'needs_review'
    renamed = store.annotate_artifact(scenes['id'], 1, {'title': '重命名'})
    assert renamed['_dependencies'] == original
    assert dependencies.artifact_status(store, renamed)['status'] == 'needs_review'
    edited = store.revise_artifact(scenes['id'], 2, scenes['items'])
    assert edited['_dependencies'] == original
    assert dependencies.artifact_status(store, edited)['status'] == 'needs_review'


def test_source_evidence_versions_survive_content_and_chunk_changes(domain):
    store, run, source, scenes, cases = domain
    old_version = scenes['_dependencies']['sources'][0]['version']
    before = store.source_snapshot(source['id'], old_version)
    chunk = store.get('chunk', source['id'] + '#P1')
    store.put('chunk', {**chunk, 'text': '完全不同的新规则'})
    assert store.get('source', source['id'])['version'] > old_version
    assert store.source_snapshot(source['id'], old_version) == before
    historical = dependencies.manifest(store, source_ids=[{'id': source['id'], 'version': old_version}])
    assert historical['sources'] == scenes['_dependencies']['sources']
    with pytest.raises(DomainError):
        dependencies.assert_manifest(store, historical)


def test_run_commit_guard_is_checked_atomically_on_creation(domain):
    store, run, source, scenes, cases = domain
    guard = dependencies.manifest(store, source_ids=[source['id']], run_id=run['id'])
    store.update_run(run['id'], _commit_guards={'analysis': guard})
    store.put('source', {**store.get('source', source['id']), '_text': '模型返回后资料改变'})
    with pytest.raises(DomainError):
        store.artifact(run['id'], 'guarded-analysis', 'analysis', '需求', [
            {'id': 'R1', 'title': '规则', 'description': '规则', 'refs': [source['id'] + '#P1']}])
    assert store.cache_get(run['id'], 'guarded-analysis') is None


def test_fresh_write_guard_never_becomes_consumed_provenance(domain):
    from tcg.context_service import artifact_context
    store, run, source, scenes, cases = domain
    chunk = store.get('chunk', source['id'] + '#P1')
    store.put('chunk', {**chunk, 'text': '未被本次模型消费的新规则'})
    fresh = dependencies.manifest(store, source_ids=[source['id']])
    pack = artifact_context(store, 'artifact_explain', scenes, scenes['items'], store.evidence([source['id']]))
    assert pack['evidence'][0]['text'] == chunk['text']
    edited = store.revise_artifact(scenes['id'], 1, scenes['items'], dependencies=fresh)
    assert edited['_dependencies'] == scenes['_dependencies']
    assert edited['_write_dependencies'] == fresh
    explained = artifact_context(store, 'artifact_explain', edited, edited['items'], store.evidence([source['id']]))
    assert [e['text'] for e in explained['evidence']] == [chunk['text']]
    explicit = store.revise_artifact(scenes['id'], 2, scenes['items'], dependencies=fresh, provenance=fresh)
    assert {ref['version'] for ref in explicit['_dependencies']['sources']} == {
        source['version'], fresh['sources'][0]['version']}


def test_creation_records_consumed_provenance_separately_from_guard(domain):
    store, run, source, scenes, cases = domain
    pinned = scenes['_dependencies']
    chunk = store.get('chunk', source['id'] + '#P1')
    store.put('chunk', {**chunk, 'text': '当前规则'})
    fresh = dependencies.manifest(store, source_ids=[source['id']])
    created = store.artifact(run['id'], 'historical-copy', 'scenarios', '历史副本', scenes['items'],
                             dependencies=fresh, provenance=pinned)
    assert created['_dependencies'] == pinned
    assert created['_write_dependencies'] == fresh
    store.update_run(run['id'], _consumed_inputs={'scenarios': pinned}, _commit_guards={'scenarios': fresh})
    automatic = store.artifact(run['id'], 'automatic-historical-copy', 'scenarios', '历史副本', scenes['items'])
    assert automatic['_dependencies'] == pinned
    assert automatic['_write_dependencies'] == fresh
