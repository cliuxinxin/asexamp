"""Explicit table edits become local evidence without creating upstream business rows."""
import copy
import json

from .dialogue_lineage import normalize_independent_rows
from .operations import native_writes
from .schemas import DomainError
from .storage import now, uid


def save_table_edit(store, business, artifact_id, expected_revision, items, *, report=None,
                    column_changes=None, profile_id=None):
    with store.transaction(), native_writes():
        artifact = store.get('artifact', artifact_id)
        if artifact['revision'] != expected_revision:
            raise DomainError('成果已更新，请刷新表格后再保存', 409)
        column_plan = None
        if column_changes:
            from .case_columns import manual_column_plan
            column_plan = manual_column_plan(store, artifact, column_changes, profile_id)
        originals = {row['id']: row for row in artifact['items']}
        rows = copy.deepcopy(items)
        # Trust persisted internal metadata only. User-authored content is captured
        # below, while source IDs and independent markers remain server-owned.
        for row in rows:
            for key in list(row):
                if key.startswith('_'):
                    row.pop(key)
            for key, value in originals.get(row.get('id'), {}).items():
                if key.startswith('_'):
                    row[key] = copy.deepcopy(value)
        changed = [row for row in rows if row != originals.get(row.get('id'))]
        source_ids, source_roles, _ = business._evidence(artifact)
        source = None
        if changed:
            authored = [{key: value for key, value in row.items()
                         if not key.startswith('_') and key != 'refs'} for row in changed]
            content = '用户在表格中直接保存的内容：\n' + json.dumps(authored, ensure_ascii=False, indent=2)
            source = store.add_source(artifact['chat_id'], '表格修改 · ' + artifact['title'],
                'change', content, [{'text': content, 'locator': '用户手动表格输入'}])
            source_ids.append(source['id'])
            source_roles[source['id']] = 'change'
            evidence_ids = [e['id'] for e in store.evidence([source['id']])]
            changed_ids = {row.get('id') for row in changed}
            for row in rows:
                if row.get('id') in changed_ids and isinstance(row.get('refs', []), list):
                    row['refs'] = list(dict.fromkeys(row.get('refs', []) + evidence_ids))
        rows = normalize_independent_rows(artifact['type'], rows, artifact['items'],
            '用户在表格中明确设置为 N/A', source_id=source['id'] if source else None)
        parents = business._parents(artifact)
        evidence = store.evidence(source_ids, source_roles)
        updated_report = copy.deepcopy(artifact.get('report', {}) if report is None else report)
        updated_report.pop('lineage', None)
        if artifact.get('report', {}).get('lineage'):
            updated_report['lineage'] = copy.deepcopy(artifact['report']['lineage'])
        updated_report.pop('_native_input_digest', None)
        if column_plan:
            updated_report['table_columns'] = copy.deepcopy(column_plan['config']['excel_columns'])
        from .native_business import artifact_profile
        business._validate(artifact['type'], rows, evidence, parents,
            artifact_profile({**artifact, 'report': updated_report}))
        guard = business._manifest(source_ids, parents + [artifact])
        updated = store.revise_artifact(artifact_id, expected_revision, rows, reason='native_manual_edit',
            report=updated_report, source_ids=source_ids, source_roles=source_roles,
            dependencies=guard, provenance=guard)
        if column_changes:
            from .case_columns import attach_manual_column_sync
            suggestion = attach_manual_column_sync(store, updated, artifact, column_changes, profile_id)
            if suggestion:
                message_id = uid('manual_columns_')
                store.put('message', {'id': message_id, 'project_id': artifact['project_id'],
                    'chat_id': artifact['chat_id'], 'role': 'assistant', 'created_at': now(),
                    'content': suggestion['message'], 'metadata': {'turn_response': {
                        'id': message_id, 'status': suggestion.get('status', 'needs_confirmation'),
                        'message': suggestion['message'], 'parts': suggestion.get('parts', []),
                        'pending': suggestion.get('pending', []), 'actions': []}}})
        return updated
