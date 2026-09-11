"""Focused pure coverage and real SQLite ancestry persistence checks.

These checks do not start FastAPI, LangGraph or a model service.
"""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.tcg.schemas import DomainError, DEFAULT_PROFILE
from backend.tcg.storage import Store, dump, now
from backend.tcg.workspace_coverage import (
    changed_scenario_ids, creation_report, resolve_related_case_artifacts,
    structural_coverage, synced_case_report, workspace_context,
)


def artifact(aid, kind, rows, parent=None, revision=1, created_at='1', visible=True):
    report = {'lineage': parent} if parent else {}
    return {'id': aid, 'type': kind, 'title': aid, 'revision': revision,
            'project_id': 'p', 'chat_id': 'chat', 'created_at': created_at,
            '_visible': visible, 'items': copy.deepcopy(rows), 'report': report}


class MemoryStore:
    def __init__(self, artifacts):
        self.artifacts = {a['id']: copy.deepcopy(a) for a in artifacts}
        self.history = {(a['id'], a['revision']): copy.deepcopy(a) for a in artifacts}
        self.cache = {}

    def get(self, kind, aid):
        if aid not in self.artifacts:
            raise DomainError('missing', 404)
        return copy.deepcopy(self.artifacts[aid])

    def list(self, kind, chat_id=None):
        return [copy.deepcopy(a) for a in self.artifacts.values() if not chat_id or a['chat_id'] == chat_id]

    def revision(self, aid, rev):
        if (aid, rev) not in self.history:
            raise DomainError('missing', 404)
        return copy.deepcopy(self.history[aid, rev])

    def cache_get(self, rid, key):
        return copy.deepcopy(self.cache.get((rid, key)))


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.analysis = artifact('a', 'analysis', [{'id': 'R1', 'title': 'login'}, {'id': 'R2', 'title': 'logout'}, {'id': 'R3', 'title': 'reset'}])
        self.scenarios = artifact('s', 'scenarios', [
            {'id': 'S1', 'title': 'login ok', 'requirement_ids': ['R1']},
            {'id': 'S2', 'title': 'logout', 'requirement_ids': ['R2']},
            {'id': 'S3', 'title': 'other', 'requirement_ids': []},
        ], {'analysis_artifact_id': 'a', 'analysis_revision': 1})
        self.cases = artifact('c', 'cases', [
            {'id': 'C1', 'title': 'login case', 'scenario_id': 'S1'},
            {'id': 'C2', 'title': 'orphan', 'scenario_id': 'S404'},
        ], {'scenario_artifact_id': 's', 'scenario_revision': 1})
        self.store = MemoryStore([self.analysis, self.scenarios, self.cases])

    def test_structural_counts_gaps_and_unassigned(self):
        result = structural_coverage(self.analysis, self.scenarios, self.cases)
        self.assertEqual(result['totals'], {'requirements': 3, 'requirements_with_scenarios': 2,
            'requirements_with_cases': 1, 'scenarios': 3, 'scenarios_with_cases': 1, 'cases': 2})
        self.assertEqual([r['status'] for r in result['requirements']], ['covered', 'missing_cases', 'missing_scenarios'])
        self.assertEqual(result['orphan_cases'][0]['id'], 'C2')
        self.assertTrue(any('未分配需求' in note for note in result['notes']))

    def test_same_row_ids_never_link_legacy_artifacts(self):
        old = copy.deepcopy(self.cases)
        old['report'] = {}
        self.store.artifacts['c'] = old
        result = workspace_context(self.store, old)
        self.assertIsNone(result['scenario_artifact_id'])
        self.assertEqual(result['coverage']['totals']['scenarios'], 0)
        self.assertTrue(all(not item['linked'] for item in result['related_artifacts']))
        self.assertEqual(result['coverage']['orphan_cases'][0]['status'], 'unlinked')
        self.assertEqual(resolve_related_case_artifacts(self.store, self.scenarios), [])
        self.assertEqual(resolve_related_case_artifacts(self.store, self.scenarios, ['c'])[0]['id'], 'c')

    def test_explicit_selection_cannot_cross_scope_or_rebind_other_branch(self):
        for updates in ({'chat_id': 'other'}, {'project_id': 'other'},
                        {'report': {'lineage': {'scenario_artifact_id': 'other'}}}, {'_visible': False}):
            self.store.artifacts['c'] = {**self.cases, **updates}
            with self.assertRaises(DomainError):
                resolve_related_case_artifacts(self.store, self.scenarios, ['c'])
        self.assertEqual(resolve_related_case_artifacts(self.store, self.scenarios), [])

    def test_case_root_and_scenario_root_count_one_branch_only(self):
        newer = artifact('c-new', 'cases', [{'id': 'C9', 'scenario_id': 'S2'}],
                         {'scenario_artifact_id': 's', 'scenario_revision': 1}, created_at='2')
        self.store.artifacts['c-new'] = newer
        current = workspace_context(self.store, self.cases)
        self.assertEqual(current['coverage']['totals']['cases'], 2)
        latest = workspace_context(self.store, self.scenarios)
        self.assertEqual(latest['selected_case_artifact_id'], 'c-new')
        self.assertEqual(latest['coverage']['totals']['cases'], 1)
        chosen = workspace_context(self.store, self.scenarios, 'c')
        self.assertEqual(chosen['coverage']['totals']['cases'], 2)
        with self.assertRaises(DomainError):
            workspace_context(self.store, self.scenarios, 'not-a-child')

    def test_changed_scenario_items_and_partial_sync_remain_precise(self):
        changed = copy.deepcopy(self.scenarios)
        changed['revision'] = 2
        changed['items'][0]['title'] = 'login with MFA'
        changed['items'][1]['title'] = 'logout everywhere'
        self.store.artifacts['s'] = changed
        self.store.history['s', 2] = copy.deepcopy(changed)
        self.assertEqual(changed_scenario_ids(self.store, changed, self.cases), ['S1', 'S2'])
        report = synced_case_report(self.store, changed, self.cases, ['S1'])
        partial = {**self.cases, 'report': report}
        self.assertEqual(report['lineage']['scenario_revision'], 1)
        self.assertEqual(report['lineage']['scenario_revisions'], {'S1': 2})
        self.assertEqual(changed_scenario_ids(self.store, changed, partial), ['S2'])
        result = workspace_context(self.store, self.cases)
        self.assertEqual(result['stale']['changed_scenario_ids'], ['S1', 'S2'])

    def test_removed_scenario_detected_and_full_sync_advances_version(self):
        changed = copy.deepcopy(self.scenarios)
        changed['revision'] = 2
        changed['items'] = changed['items'][1:]
        self.store.history['s', 2] = changed
        self.assertIn('S1', changed_scenario_ids(self.store, changed, self.cases))
        report = synced_case_report(self.store, changed, self.cases, ['S1', 'S2', 'S3', 'S404'])
        self.assertEqual(report['lineage']['scenario_revision'], 2)
        self.assertNotIn('scenario_revisions', report['lineage'])

    def test_missing_baseline_and_partial_legacy_sync_do_not_clear_other_drift(self):
        old = {**self.cases, 'report': {}}
        report = synced_case_report(self.store, self.scenarios, old, ['S1'])
        self.assertNotIn('scenario_revision', report['lineage'])
        self.assertEqual(changed_scenario_ids(self.store, self.scenarios, {**old, 'report': report}), ['S2', 'S3', 'S404'])

    def test_creation_uses_exact_parent_cache_then_snapshot_never_report(self):
        run = {'id': 'run', 'project_id': 'p', 'chat_id': 'chat', '_artifact_snapshot': self.scenarios}
        self.store.cache['run', 'workspace:scenario_parent'] = {'id': 's', 'revision': 1}
        forged = {'lineage': {'scenario_artifact_id': 'other', 'scenario_revision': 999}, 'summary': 'keep'}
        result = creation_report(self.store, run, 'cases', forged)
        self.assertEqual(result['lineage'], {'scenario_artifact_id': 's', 'scenario_revision': 1})
        self.assertEqual(result['summary'], 'keep')
        self.store.cache.clear()
        self.assertEqual(creation_report(self.store, run, 'cases')['lineage']['scenario_artifact_id'], 's')
        run['_artifact_snapshot'] = None
        self.assertNotIn('lineage', creation_report(self.store, run, 'cases',
            {'lineage': {'scenario_artifact_id': 's', 'scenario_revision': 1}}))

    def test_analysis_revision_change_is_visible(self):
        self.store.artifacts['a']['revision'] = 2
        result = workspace_context(self.store, self.cases)
        self.assertTrue(result['stale']['analysis'])
        self.assertFalse(result['stale']['scenarios'])

    def test_workspace_sources_show_only_new_active_same_chat_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                project = store.list('project')[0]
                chat = store.create_chat(project['id'], 'workspace')
                sibling = store.create_chat(project['id'], 'other chat')
                other_project = store.create_project('other project')
                other_chat = store.create_chat(other_project['id'], 'other project chat')
                def source(target, name, role='supplement'):
                    return store.add_source(target['id'], name, role, 'private requirement text',
                                            [{'text': 'private requirement text', 'location': 'P1'}])
                current = source(chat, 'existing')
                added = source(chat, 'new material')
                source(chat, 'format only', 'example')
                inactive = source(chat, 'inactive')
                store.deactivate_source(inactive['id'])
                source(sibling, 'other chat material')
                source(other_chat, 'other project material')
                target = {**artifact('workspace-source-test', 'cases', []),
                          'project_id': project['id'], 'chat_id': chat['id'],
                          '_source_ids': [current['id']]}
                store.put('artifact', target)
                response = workspace_context(store, target)
                self.assertEqual(response['sources'], [{'id': added['id'], 'name': 'new material',
                    'role': 'supplement', 'characters': len('private requirement text')}])
                self.assertNotIn('_text', response['sources'][0])
            finally:
                store.close()

    def test_store_creation_hook_persists_lineage_in_first_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                project = store.list('project')[0]
                chat = store.create_chat(project['id'], 'coverage')
                source = store.add_source(chat['id'], 'login', 'primary', 'login', [{'text': 'login', 'location': 'P1'}])
                ref = source['id'] + '#P1'
                run = {'id': 'run', 'project_id': project['id'], 'chat_id': chat['id'],
                       'status': 'running', 'stage': 'scenarios', '_source_ids': [source['id']],
                       '_profile': DEFAULT_PROFILE, '_artifact_snapshot': None}
                store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', ('run', chat['id'], project['id'], 'running', dump(run)))
                analysis = store.artifact('run', 'analysis', 'analysis', 'req', [
                    {'id': 'R1', 'title': 'login', 'description': 'login', 'refs': [ref]}])
                store.cache_set('run', 'workspace:analysis_parent', {'id': analysis['id'], 'revision': 1})
                scenarios = store.artifact('run', 'scenarios', 'scenarios', 'scenarios', [
                    {'id': 'S1', 'title': 'login', 'description': 'login', 'priority': 'P1',
                     'requirement_ids': ['R1'], 'refs': [ref]}])
                expected = {'analysis_artifact_id': analysis['id'], 'analysis_revision': 1}
                self.assertEqual(scenarios['report']['lineage'], expected)
                self.assertEqual(store.revision(scenarios['id'], 1)['report']['lineage'], expected)
                store.cache_set('run', 'workspace:scenario_parent', {'id': scenarios['id'], 'revision': 1})
                cases = store.artifact('run', 'cases', 'cases', 'cases', [
                    {'id': 'C1', 'title': 'login', 'scenario_id': 'S1', 'type': 'Business', 'priority': 'P1',
                     'preconditions': '', 'steps': [{'action': 'login', 'expected': 'ok'}], 'refs': [ref]}])
                self.assertEqual(cases['report']['lineage']['scenario_artifact_id'], scenarios['id'])
                self.assertEqual(store.artifact('run', 'cases', 'cases', 'unused', [])['id'], cases['id'])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
