"""Bind generation batches to inputs without confusing freshness and provenance."""
import copy

from .dependencies import assert_manifest, digest, manifest
from .schemas import DomainError


OUTPUTS = {'analyze_requirement': 'analysis', 'generate_scenarios': 'scenarios',
           'generate_cases': 'cases', 'import_cases': 'cases', 'direct_cases': 'cases',
           'review_cases': 'cases', 'modify': 'cases', 'complete_case_fields': 'cases'}


def merge_manifests(*values, strict=False):
    result = {'version': 1, 'artifacts': [], 'sources': [], 'profiles': []}
    for group in ('artifacts', 'sources', 'profiles'):
        refs = {}
        for value in values:
            for ref in (value or {}).get(group, []):
                key = (ref['id'], ref.get('revision', ref.get('version')))
                if strict and any(k[0] == key[0] and k != key for k in refs):
                    raise DomainError('生成批次的输入版本已变化，请重新生成受影响阶段', 409)
                refs[key] = copy.deepcopy(ref)
        result[group] = [refs[key] for key in sorted(refs)]
    for value in values:
        if (value or {}).get('run'):
            if strict and result.get('run') and result['run'] != value['run']:
                raise DomainError('生成范围已改变，请重新生成受影响阶段', 409)
            result['run'] = copy.deepcopy(value['run'])
    result['digest'] = digest(result)
    return result


def begin_generation(store, run_id, task, context):
    if not run_id or task not in OUTPUTS:
        return None
    with store.transaction():
        run = store.run(run_id)
        kind = (context.get('artifact') or {}).get('type', OUTPUTS[task]) if task == 'modify' else OUTPUTS[task]
        consumed = context.get('dependency_manifest')
        if not consumed:
            refs = []
            for evidence in context.get('evidence', []):
                if evidence.get('source_id'):
                    refs.append({'id': evidence['source_id'], 'version': evidence['source_version']}
                                if evidence.get('source_version') else evidence['source_id'])
            consumed = manifest(store, source_ids=refs)
        # Historical versions may be read deliberately. Guard their current heads,
        # while keeping the versions actually supplied in a separate manifest.
        guard = manifest(store, artifact_ids=[ref['id'] for ref in consumed.get('artifacts', [])],
                         source_ids=list(dict.fromkeys(run['_source_ids'] +
                             [ref['id'] for ref in consumed.get('sources', [])])), run_id=run_id)
        # Field completion and review consume earlier generation in the same
        # input epoch. Changing the task name cannot reset its source guards.
        epoch = [run.get('input_version', 0), run.get('_edit_token')]
        previous = run.get('_generation_epochs', {}).get(kind)
        if previous == epoch:
            saved = run.get('_commit_guards', {}).get(kind)
            if saved:
                assert_manifest(store, saved)
                guard = merge_manifests(saved, guard, strict=True)
            consumed = merge_manifests(run.get('_consumed_inputs', {}).get(kind), consumed)
        store.update_run(run_id,
            _generation_epochs={**run.get('_generation_epochs', {}), kind: epoch},
            _commit_guards={**run.get('_commit_guards', {}), kind: guard},
            _consumed_inputs={**run.get('_consumed_inputs', {}), kind: consumed})
        return guard


def commit_arguments(store, run_id, kind):
    run = store.run(run_id)
    return {'dependencies': run.get('_commit_guards', {}).get(kind),
            'provenance': run.get('_consumed_inputs', {}).get(kind)}
