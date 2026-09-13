"""Pure saved-artifact comparisons for table projections."""
import hashlib
import json
import re
from .schemas import DomainError

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def item_diff(before, after):
    old, new = {r['id']: r for r in before}, {r['id']: r for r in after}
    return {'added': [i for i in new if i not in old],
            'updated': [i for i in new if i in old and new[i] != old[i]],
            'deleted': [i for i in old if i not in new]}


def changed_requirement_ids(store, analysis, scenarios):
    from .workspace_coverage import changed_scenario_ids
    # The same version comparison applies to requirement→scenario lineage.
    source = scenarios.get('report', {}).get('lineage', {})
    translated = {'scenario_artifact_id': source.get('analysis_artifact_id'),
                  'scenario_revision': source.get('analysis_revision'),
                  'scenario_revisions': source.get('analysis_revisions', {})}
    rows = [{'scenario_id': rid} for row in scenarios['items'] for rid in row.get('requirement_ids', [])]
    changed = set(changed_scenario_ids(store, analysis, {'items': rows, 'report': {'lineage': translated}}))
    # Business rules can change while requirement rows stay byte-for-byte equal.
    # Summary/diagram wording and other presentation metadata are not inputs here.
    def rules(artifact):
        report = artifact.get('report') or {}
        return {key: report.get(key) or ({} if key == 'requirement_map' else [])
                for key in ('requirement_map', 'global_rules', 'relationships', 'conflicts',
                            'in_scope', 'out_of_scope', 'assumptions')}
    current_rules, baselines = rules(analysis), {}
    versions = source.get('analysis_revisions') or {}
    for row in analysis['items']:
        revision = versions.get(row['id'], source.get('analysis_revision'))
        if type(revision) is not int:
            continue  # Unknown row lineage is already reported by the row check.
        if revision not in baselines:
            try:
                baselines[revision] = rules(store.revision(analysis['id'], revision))
            except DomainError as exc:
                if exc.status != 404:
                    raise
                baselines[revision] = None
        if baselines[revision] != current_rules:
            changed.add(row['id'])
    return sorted(changed)


def review_details(store, cases):
    if not cases or not cases.get('report', {}).get('review_reports'):
        return {'status': 'missing', 'revision': None, 'changed_item_ids': []}
    reports = cases['report']['review_reports']
    baseline = cases
    for revision in range(cases['revision'] - 1, 0, -1):
        try:
            previous = store.revision(cases['id'], revision)
        except DomainError:
            return {'status': 'stale', 'revision': None, 'changed_item_ids': [r['id'] for r in cases['items']]}
        if previous.get('report', {}).get('review_reports') != reports:
            break
        baseline = previous
    from .case_fields import MANUAL_FIELDS
    manual = {column['field'] for column in cases.get('_profile', {}).get('excel_columns', [])
              if column.get('value_source') in ('manual', 'default')}
    def design(artifact):
        return {row['id']: {key: value for key, value in row.items()
                if key not in manual and re.sub(r'[\s_-]', '', key).lower() not in MANUAL_FIELDS}
                for row in artifact['items']}
    current, old = design(cases), design(baseline)
    changed = {item_id for item_id in current.keys() | old.keys() if current.get(item_id) != old.get(item_id)}
    reviewed = set()
    for report in reports:
        scope = report.get('scope')
        if not scope or scope.get('all'):
            reviewed.update(old)
        else:
            reviewed.update(scope.get('case_ids', []))
    unreviewed = set(current) - reviewed
    return {'status': 'stale' if changed else 'partial' if unreviewed else 'current',
            'revision': baseline['revision'], 'changed_item_ids': sorted(changed | unreviewed),
            'unreviewed_item_ids': sorted(unreviewed)}
