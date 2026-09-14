"""Traceability uses exact branches and parent row snapshots without model calls."""
import copy

import pytest

from tcg.schemas import DomainError
from tcg.storage import Store, dump
from tcg.traceability import project_traceability


@pytest.fixture
def graph(tmp_path):
    store = Store(tmp_path)
    project = store.create_project('追溯项目')
    chat = store.create_chat(project['id'], '登录测试')

    def save(aid, kind, items, lineage=None, revision=1, chat_id=None, visible=True):
        value = {'id': aid, 'project_id': project['id'], 'chat_id': chat_id or chat['id'],
                 'title': aid, 'type': kind, 'items': copy.deepcopy(items), 'revision': revision,
                 'report': {'lineage': lineage or {}}, '_visible': visible}
        store.db.execute('INSERT INTO revisions VALUES (?,?,?,?,?,?)',
                         (aid, revision, dump(value), '', 'test', '{}'))
        store.put('artifact', value)
        return value

    analysis = save('a', 'analysis', [{'id': 'R1', 'title': '登录'}, {'id': 'R2', 'title': '退出'}])
    scenarios = save('s', 'scenarios', [
        {'id': 'S1', 'title': '登录成功', 'requirement_ids': ['R1']},
        {'id': 'S2', 'title': '退出成功', 'requirement_ids': ['R2']}],
        {'analysis_artifact_id': 'a', 'analysis_revision': 1})
    cases = save('c', 'cases', [{'id': 'C1', 'title': '验证登录', 'scenario_id': 'S1'},
        {'id': 'C2', 'title': '验证退出', 'scenario_id': 'S2'}],
        {'scenario_artifact_id': 's', 'scenario_revision': 1})
    yield store, project, chat, save, analysis, scenarios, cases
    store.close()


def get_nodes(data):
    return {(row['artifact_id'], row['item_id']): row for chat in data['chats'] for row in chat['nodes']}


def test_complete_graph_preserves_versioned_identity_without_reading_document_text(graph, monkeypatch):
    store, project, chat, *_ = graph
    monkeypatch.setattr(store, 'evidence', lambda *args, **kwargs: pytest.fail('No source text needed'))
    result = project_traceability(store, project['id'], chat['id'])
    rows = get_nodes(result)
    assert result['summary'] == {'requirements': 2, 'scenarios': 2, 'cases': 2,
        'missing': 0, 'stale': 0, 'independent': 0, 'direct_cases': 0}
    assert rows['s', 'S1']['parent_keys'] == [rows['a', 'R1']['key']]
    assert rows['c', 'C1']['parent_keys'] == [rows['s', 'S1']['key']]
    assert [(p['item_id'], p['revision']) for p in rows['c', 'C1']['basis']] == [('S1', 1), ('R1', 1)]
    assert all('items' not in row and 'refs' not in row and 'steps' not in row for row in rows.values())


def test_missing_downstream_and_unknown_upstream_are_visible_without_guessed_links(graph):
    store, project, chat, save, analysis, scenarios, cases = graph
    save('c', 'cases', [cases['items'][0]], cases['report']['lineage'], 2)
    save('orphan', 'cases', [{'id': 'C1', 'title': '另一分支', 'scenario_id': 'S1'}])
    rows = get_nodes(project_traceability(store, project['id'], chat['id']))
    assert 'missing_cases' in rows['s', 'S2']['statuses']
    assert 'missing_cases' in rows['a', 'R2']['statuses']
    assert rows['orphan', 'C1']['parent_keys'] == []
    assert 'missing_parent' in rows['orphan', 'C1']['statuses']


def test_only_children_of_changed_row_become_stale_then_partial_sync_clears_them(graph):
    store, project, chat, save, analysis, scenarios, cases = graph
    items = copy.deepcopy(scenarios['items']); items[0]['title'] = '更新登录行为'
    save('s', 'scenarios', items, scenarios['report']['lineage'], 2)
    result = project_traceability(store, project['id'], chat['id'])
    rows = get_nodes(result)
    assert rows['c', 'C1']['stale']
    assert not rows['c', 'C2']['stale']
    assert not rows['s', 'S1']['stale']  # Editing a row is not itself upstream drift.
    assert result['summary']['stale'] == 1
    report = {**cases['report']['lineage'], 'scenario_item_revisions': {'C1': 2}}
    save('c', 'cases', cases['items'], report, 2)
    assert project_traceability(store, project['id'], chat['id'])['summary']['stale'] == 0


def test_case_keeps_historical_requirement_basis_when_scenario_lineage_advances(graph):
    store, project, chat, save, analysis, scenarios, cases = graph
    items = copy.deepcopy(analysis['items']); items[0]['title'] = '需求改变'
    save('a', 'analysis', items, revision=2)
    # Scenario content may be unchanged while its reviewed requirement baseline advances.
    save('s', 'scenarios', scenarios['items'], {'analysis_artifact_id': 'a', 'analysis_revision': 2}, 2)
    rows = get_nodes(project_traceability(store, project['id'], chat['id']))
    assert not rows['s', 'S1']['stale']
    assert rows['c', 'C1']['stale']
    assert not rows['c', 'C2']['stale']
    assert rows['c', 'C1']['basis'][-1]['revision'] == 1


def test_independent_and_explicit_direct_cases_are_distinct_from_missing_parent(graph):
    store, project, chat, save, *_ = graph
    marker = {'reason': '用户明确独立新增'}
    save('ind', 'cases', [{'id': 'C-independent', 'title': '独立用例', 'scenario_id': '', '_independent_origin': marker}])
    save('direct', 'cases', [{'id': 'C-direct', 'title': '直接根据需求', 'scenario_id': '',
        'requirement_ids': ['R1'], '_independent_origin': {'reason': '用户跳过场景', 'mode': 'direct_requirements'}}],
        {'generation_mode': 'direct_requirements', 'analysis_artifact_id': 'a', 'analysis_revision': 1})
    rows = get_nodes(project_traceability(store, project['id'], chat['id']))
    assert rows['ind', 'C-independent']['independent'] and not rows['ind', 'C-independent']['missing']
    direct = rows['direct', 'C-direct']
    assert direct['parent_keys'] == [rows['a', 'R1']['key']]
    assert direct['direct'] and not direct['missing'] and not direct['independent']
    assert 'skipped_scenarios' in direct['statuses']


def test_multiple_parents_count_once_and_duplicate_ids_do_not_merge_across_branches_or_chats(graph):
    store, project, chat, save, *_ = graph
    save('multi', 'scenarios', [{'id': 'S1', 'title': '跨需求', 'requirement_ids': ['R1', 'R2']}],
         {'analysis_artifact_id': 'a', 'analysis_revision': 1})
    second = store.create_chat(project['id'], '另一会话')
    save('other-a', 'analysis', [{'id': 'R1', 'title': '其他登录'}], chat_id=second['id'])
    save('other-s', 'scenarios', [{'id': 'S1', 'title': '其他场景', 'requirement_ids': ['R1']}],
         {'analysis_artifact_id': 'other-a', 'analysis_revision': 1}, chat_id=second['id'])
    result = project_traceability(store, project['id'])
    rows = get_nodes(result)
    assert result['summary']['requirements'] == 3 and result['summary']['scenarios'] == 4
    assert len(rows['multi', 'S1']['parent_keys']) == 2
    assert rows['other-s', 'S1']['parent_keys'] == [rows['other-a', 'R1']['key']]
    assert rows['other-s', 'S1']['key'] != rows['s', 'S1']['key']
    assert len(project_traceability(store, project['id'], chat['id'])['chats']) == 1


def test_empty_invisible_data_and_cross_project_scope(graph):
    store, project, chat, save, *_ = graph
    empty = store.create_chat(project['id'], '尚未生成')
    save('hidden', 'analysis', [{'id': 'R-hidden', 'title': '未发布'}], chat_id=empty['id'], visible=False)
    result = project_traceability(store, project['id'], empty['id'])
    assert not result['chats'][0]['nodes']
    assert not any(result['summary'].values())
    foreign = store.create_project('别的项目')
    with pytest.raises(DomainError) as exc:
        project_traceability(store, foreign['id'], chat['id'])
    assert exc.value.status == 404
