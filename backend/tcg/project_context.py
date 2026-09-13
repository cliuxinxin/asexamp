"""Pure project template, sample and shared-fact services for native tools and REST."""
import copy
import json

from .schemas import DomainError
from .storage import now

MAX_SAMPLES = 5
MAX_SAMPLE_CHARACTERS = 12000
MAX_SHARED_SOURCES = 100
MAX_SHARED_CHARACTERS = 200000


def merge_template_config(current, proposal, kinds):
    """Apply only recognized template families, keeping manual/default ownership."""
    from .schemas import DEFAULT_PROFILE, profile_config
    from .case_fields import template_columns, MANUAL_FIELDS
    import re
    if not isinstance(proposal, dict) or not isinstance(kinds, list) or not kinds or any(
            kind not in ('scenarios', 'cases') for kind in kinds):
        raise DomainError('模板建议需要 config 对象及 scenarios/cases 类型')
    scenario_keys = {'scenario_excel_columns', 'scenario_sheet_name', 'scenario_filename_pattern'}
    case_keys = {'excel_columns', 'excel_layout', 'sheet_name', 'filename_pattern',
                 'template_rules', 'case_level', 'case_types', 'additional_rules'}
    allowed = (scenario_keys if 'scenarios' in kinds else set()) | (case_keys if 'cases' in kinds else set())
    result, notes = copy.deepcopy(current), []
    for key, value in proposal.items():
        if key not in allowed:
            continue
        if value is None or value == [] or (isinstance(value, str) and not value.strip()):
            notes.append(f'{key} 未识别到有效值，保留当前设置')
            continue
        column_key = 'scenario_excel_columns' if key in scenario_keys else 'excel_columns'
        if proposal.get(column_key) == [] and value == DEFAULT_PROFILE.get(key):
            continue
        result[key] = '\n'.join(map(str, value)) if key == 'template_rules' and isinstance(value, list) else copy.deepcopy(value)
    # Validate before inspecting model-provided column objects.
    result = profile_config(result)
    old_policies = {c['field']: c for c in template_columns(current)}
    for column in result['excel_columns']:
        previous = old_policies.get(column['field'], {})
        manual_name = any(re.sub(r'[\s_-]', '', label).lower() in MANUAL_FIELDS
                          for label in (column['field'], column.get('header', '')))
        if previous.get('value_source') in ('manual', 'default'):
            for key in ('value_source', 'required', 'default_value'):
                if key in previous:
                    column[key] = copy.deepcopy(previous[key])
        elif manual_name:
            column['value_source'] = 'manual'
            column['required'] = False
    return profile_config(result), notes


def validate_sample_cases(value):
    if not isinstance(value, list) or len(value) > MAX_SAMPLES:
        raise DomainError('固定格式样例最多 5 条')
    if len(json.dumps(value, ensure_ascii=False)) > MAX_SAMPLE_CHARACTERS:
        raise DomainError('固定格式样例超过 12000 字符，请减少所选用例')
    forbidden = {'refs', 'source_ids', 'requirement_ids', 'scenario_id', 'project_id', 'chat_id'}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get('title'), str) or not row['title'].strip():
            raise DomainError('格式样例需要非空标题')
        if any(key.startswith('_') or key in forbidden for key in row):
            raise DomainError('格式样例不能保存业务引用或内部记录')
        steps = row.get('steps')
        if not isinstance(steps, list) or not steps or any(not isinstance(step, dict)
                or not isinstance(step.get('action'), str) or not isinstance(step.get('expected'), str)
                for step in steps):
            raise DomainError('格式样例需要 action / expected 步骤数组')
    return value


def shared_sources(store, project_id, scope=None):
    from .project_facts import normalize_scope, scope_matches
    scope = normalize_scope(scope)
    return [source for source in store.list('source', project_id=project_id)
            if source.get('_project_shared') and source.get('_active')
            and source['role'] == 'clarification' and source.get('status', 'confirmed') == 'confirmed'
            and not source.get('_task_only') and scope_matches(source.get('scope', {}), scope)]


def share_clarification(store, source_id, project_id, *, scope=None, fact_key=None, supersedes=None, provenance=None):
    """Called only after an explicit clarification submission; safe on retries."""
    from .project_facts import normalize_scope, check_fact_conflicts
    with store.transaction():
        source = store.get('source', source_id)
        if source['project_id'] != project_id or source['role'] != 'clarification':
            raise DomainError('只有本项目的已提交澄清可以共享')
        if source.get('status') in ('provisional', 'superseded') or source.get('_task_only'):
            raise DomainError('待确认假设或已替代规则不能直接共享；请明确确认新的业务结论')
        if not source.get('_active'):
            raise DomainError('已停用的澄清不能共享')
        scope = normalize_scope(scope if scope is not None else source.get('scope'))
        fact_key = fact_key if fact_key is not None else source.get('fact_key', '')
        if not isinstance(fact_key, str) or len(fact_key) > 500:
            raise DomainError('规则主题最多 500 字符')
        supersedes = supersedes if supersedes is not None else source.get('supersedes', [])
        if not isinstance(supersedes, list) or len(supersedes) > 20 or any(not isinstance(sid, str) for sid in supersedes) or len(set(supersedes)) != len(supersedes):
            raise DomainError('被替代规则需要不重复的来源 ID，最多 20 条')
        previous = []
        for sid in supersedes:
            old = store.get('source', sid)
            if sid == source_id or old['project_id'] != project_id or old['role'] != 'clarification':
                raise DomainError('只能替代同一项目的其他业务澄清')
            if old.get('status') == 'provisional' or old.get('superseded_by', old.get('_superseded_by')) not in (None, source_id):
                raise DomainError('被替代规则已改变，请查看具体规则后再确认', 409)
            if old.get('scope', {}).get('module') and scope.get('module') and old['scope']['module'] != scope['module']:
                raise DomainError('新旧规则适用模块不同，请明确该模块的规则')
            previous.append(old)
        if source.get('_project_shared'):
            return source
        others = shared_sources(store, project_id)
        claims = source.get('claims') or ([{'key': fact_key, 'content': source['_text']}] if fact_key else [])
        check_fact_conflicts(others, source, scope, claims, supersedes)
        remaining = [s for s in others if s['id'] not in supersedes]
        if len(remaining) >= MAX_SHARED_SOURCES or sum(s['characters'] for s in remaining) + source['characters'] > MAX_SHARED_CHARACTERS:
            raise DomainError('项目共享澄清超过 100 条或 20 万字符；请先移除过期共享内容，或取消保存到项目')
        confirmed_at = now()
        details = {**(source.get('provenance') or {}), **(provenance or {}),
                   'source_id': source_id, 'chat_id': source['chat_id']}
        details.setdefault('confirmed_by', '当前用户')
        details.setdefault('confirmed_at', confirmed_at)
        details.setdefault('origin', 'clarification_submission')
        source = store.put('source', {**source, 'status': 'confirmed', 'scope': scope,
            'fact_key': fact_key.strip(), 'claims': claims, 'provenance': details, 'supersedes': supersedes,
            '_project_shared': True, '_shared_at': confirmed_at})
        for old in previous:
            store.put('source', {**old, 'status': 'superseded', 'superseded_by': source_id,
                '_superseded_by': source_id, '_project_shared': False, '_active': False})
        store.audit(source_id, 'project_clarification_shared', {'project_id': project_id, 'scope': scope, 'supersedes': supersedes})
        return source


def shared_context(store, project_id, scope=None):
    from .project_facts import fact_record, scope_matches
    store.get('project', project_id)
    return {'clarifications': [fact_record(s) for s in shared_sources(store, project_id, scope)],
        'fact_history': [fact_record(s) for s in store.list('source', project_id=project_id)
                         if s['role'] == 'clarification' and (s.get('status') == 'superseded' or s.get('_superseded_by'))
                         and scope_matches(s.get('scope', {}), scope)],
        'samples': [{'profile_id': p['id'], 'profile_name': p['name'], 'version': p['version'],
                     'count': len(p['config'].get('sample_cases', []))}
                    for p in store.list('profile', project_id=project_id)]}


def unshare_clarification(store, project_id, source_id):
    with store.transaction():
        source = store.get('source', source_id)
        if source['project_id'] != project_id or not source.get('_project_shared'):
            raise DomainError('未找到本项目的共享澄清', 404)
        store.put('source', {**source, '_project_shared': False})
        store.audit(source_id, 'project_clarification_unshared', {'project_id': project_id})
    return {'ok': True}


def pin_samples(store, artifact_id, profile_id, expected_version, selected_ids):
    with store.transaction():
        artifact = store.get('artifact', artifact_id)
        profile = store.get('profile', profile_id)
        if artifact['type'] != 'cases' or not artifact.get('_visible'):
            raise DomainError('请选择已发布的测试用例')
        if profile['project_id'] != artifact['project_id']:
            raise DomainError('Profile 与用例必须属于同一项目')
        if not isinstance(selected_ids, list) or not 1 <= len(selected_ids) <= MAX_SAMPLES or len(set(selected_ids)) != len(selected_ids):
            raise DomainError('请选择 1 至 5 条不重复的样例用例')
        rows = {row['id']: row for row in artifact['items']}
        if not set(selected_ids) <= rows.keys():
            raise DomainError('所选用例不属于当前结果')
        dropped = {'refs', 'source_ids', 'requirement_ids', 'scenario_id', 'project_id', 'chat_id', 'id'}
        from .case_fields import MANUAL_FIELDS, template_columns
        import re
        dropped.update(column['field'] for column in template_columns(artifact.get('_profile', {})) + template_columns(profile['config'])
                       if column['value_source'] == 'manual')
        dropped.update(key for item_id in selected_ids for key in rows[item_id]
                       if re.sub(r'[\s_-]', '', key).lower() in MANUAL_FIELDS)
        samples = [{key: copy.deepcopy(value) for key, value in rows[item_id].items()
                    if not key.startswith('_') and key not in dropped} for item_id in selected_ids]
        validate_sample_cases(samples)
        return store.update_profile(profile_id, profile['name'],
            {**profile['config'], 'sample_cases': samples}, expected_version)
