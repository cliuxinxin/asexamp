"""Durable incremental work and bounded, explicitly scoped model inputs."""

import copy
import hashlib
import json

from .schemas import DomainError
from .storage import now


def fingerprint(value) -> str:
    """Hash JSON content independently of dictionary insertion order."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _budget(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainError('预算必须为正整数；请提供可容纳元数据和正文的字符预算')


def _size(value):
    # Match ModelClient's HumanMessage serialization, including separators.
    return len(json.dumps(value, ensure_ascii=False))


def _parts(item, budget):
    if _size([item]) <= budget:
        return [copy.deepcopy(item)]
    text = item.get('text')
    if not isinstance(text, str):
        raise DomainError('证据正文必须为字符串；请重新导入来源')
    original_offset = item.get('excerpt', {}).get('start', 0)
    original_total = item.get('excerpt', {}).get('total', len(text))
    parts, offset = [], 0
    while offset < len(text):
        # The serialized representation cannot be shorter than its text. This
        # bound avoids repeatedly copying the remainder of a large document.
        low, high, best = offset + 1, min(len(text), offset + budget), None
        while low <= high:
            end = (low + high) // 2
            trial = {**item, 'text': text[offset:end],
                     'excerpt': {'start': original_offset + offset, 'end': original_offset + end,
                                 'total': original_total}}
            if _size([trial]) <= budget:
                best = trial
                low = end + 1
            else:
                high = end - 1
        if best is None:
            raise DomainError(f'证据元数据及至少一个正文字符超过 {budget} 字符预算；'
                              '请提高读取预算或缩短来源名称和位置信息')
        parts.append(copy.deepcopy(best))
        offset = best['excerpt']['end'] - original_offset
    if not parts:
        raise DomainError(f'证据元数据超过 {budget} 字符预算；请提高读取预算或缩短位置信息')
    return parts


def split_evidence(evidence: list[dict], budget: int = 6000) -> list[list[dict]]:
    """Return every character in bounded groups without mixing sources.

    Normal paragraphs stay intact. Oversized paragraphs keep their original
    reference and receive zero-based, end-exclusive ``excerpt`` offsets.
    Budgets count model JSON characters, including escaping and metadata.
    """
    _budget(budget)
    groups, group = [], []
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            raise DomainError('证据必须为包含 text 字符串的对象；请重新导入来源')
        for part in _parts(item, budget):
            if group and (group[0].get('source_id') != part.get('source_id') or
                          _size(group + [part]) > budget):
                groups.append(group)
                group = []
            group.append(part)
    if group:
        groups.append(group)
    return groups


_PUBLIC_FIELDS = ('id', 'key', 'kind', 'title', 'status', 'refs', 'artifact_id', 'attempt', 'error')

_PROFILE_FIELDS = {
    'analyze': ('scope', 'language', 'additional_rules'),
    'scenarios': ('scenario_schema', 'scenario_level', 'additional_rules'),
    'cases': ('case_schema', 'case_level', 'case_types', 'template', 'additional_rules'),
    'review': ('review_rules', 'review_dimensions', 'review_schema'),
}

_TASK_GROUPS = {
    'analysis': 'analyze', 'analyze_requirement': 'analyze', 'review_requirement': 'analyze',
    'links': 'analyze', 'impact': 'analyze',
    'scenario': 'scenarios', 'generate_scenario': 'scenarios', 'generate_scenarios': 'scenarios',
    'case': 'cases', 'generate_case': 'cases', 'generate_cases': 'cases',
    'modify': 'cases', 'import': 'cases', 'import_cases': 'cases', 'template': 'cases',
    'learn_template': 'cases', 'review_case': 'review', 'review_cases': 'review',
}


class Workspace:
    def __init__(self, store):
        self.store = store

    def units(self, run: dict, budget: int = 6000) -> list[dict]:
        """Read each currently authorized source separately and cover it fully."""
        _budget(budget)
        current = self.store.run(run['id'])
        selected = run.get('_source_ids', [])
        if (not isinstance(selected, list) or any(not isinstance(s, str) for s in selected) or
                not set(selected).issubset(current.get('_source_ids', []))):
            raise DomainError('来源不在当前运行授权范围内；请刷新任务后重试', 409)
        result = []
        for source_id in dict.fromkeys(selected):
            source = self.store.get('source', source_id)
            if source.get('project_id') != current['project_id']:
                raise DomainError('来源不属于当前项目', 403)
            if not source.get('_active', True) or source.get('role') == 'example':
                continue
            evidence = [{**item, 'role': source['role']} for item in self.store.evidence([source_id])
                        if item.get('role') != 'example']
            groups = split_evidence(evidence, budget)
            for index, group in enumerate(groups, 1):
                refs = list(dict.fromkeys(item['id'] for item in group))
                title = source.get('name') or source_id
                if len(groups) > 1:
                    title = f'{title} · {index}/{len(groups)}'
                result.append({'id': 'unit_' + fingerprint(group), 'title': title,
                               'evidence': group, 'refs': refs, 'source_ids': [source_id]})
        return result

    @staticmethod
    def _id(run_id, key):
        return 'work_' + fingerprint([run_id, key])

    def get(self, run_id: str, key: str) -> dict | None:
        self.store.run(run_id)
        try:
            return self.store.get('agent_work', self._id(run_id, key))
        except DomainError as error:
            if error.status == 404:
                return None
            raise

    def _save(self, run_id, value):
        value['updated_at'] = now()
        self.store.put('agent_work', value)
        view = self.view(run_id)
        self.store.append_event(run_id, 'agent_work', {**view, 'items': view['items'][-20:]})
        return copy.deepcopy(value)

    def begin(self, run_id: str, key: str, kind: str, title: str,
              refs: list[str] = (), dependencies: list[str] = ()) -> dict:
        with self.store.transaction():
            self.store.assert_running(run_id)
            existing = self.get(run_id, key)
            if existing and existing['status'] == 'completed':
                return existing
            run = self.store.run(run_id)
            value = {**(existing or {}), 'id': self._id(run_id, key), 'run_id': run_id,
                     'project_id': run['project_id'], 'chat_id': run['chat_id'],
                     'key': key, 'kind': kind, 'title': title, 'status': 'running',
                     'refs': list(refs), 'dependencies': list(dependencies), 'error': None,
                     'artifact_id': None, 'attempt': (existing or {}).get('attempt', 0) + 1,
                     'created_at': (existing or {}).get('created_at', now())}
            return self._save(run_id, value)

    def _existing(self, run_id, key):
        value = self.get(run_id, key)
        if value is None:
            raise DomainError('未找到工作项；请先开始该工作项再提交结果', 404)
        return value

    def accept(self, run_id: str, key: str, result: dict, artifact_id: str | None = None) -> dict:
        with self.store.transaction():
            self.store.assert_running(run_id)
            value = self._existing(run_id, key)
            if value['status'] == 'completed':
                return value
            if not isinstance(result, dict):
                raise DomainError('工作项结果必须为对象')
            value.update(status='completed', _result=copy.deepcopy(result),
                         artifact_id=artifact_id, error=None)
            return self._save(run_id, value)

    def fail(self, run_id: str, key: str, error: str) -> dict:
        with self.store.transaction():
            self.store.assert_running(run_id)
            value = self._existing(run_id, key)
            if value['status'] == 'completed':
                return value
            value.update(status='failed', error=error)
            return self._save(run_id, value)

    def list(self, run_id: str) -> list[dict]:
        with self.store.lock:
            run = self.store.run(run_id)
            records = self.store.list('agent_work', project_id=run['project_id'], chat_id=run['chat_id'])
            scoped = [value for value in records if value.get('run_id') == run_id]
            scoped.sort(key=lambda value: (value['created_at'], value['id']))
            return [{**{field: copy.deepcopy(value.get(field)) for field in _PUBLIC_FIELDS},
                     'status': 'superseded' if value.get('_superseded') else value['status']} for value in scoped]

    def view(self, run_id: str) -> dict:
        items = self.list(run_id)
        current = next((item for item in reversed(items) if item['status'] == 'running'), None)
        return {'completed': sum(item['status'] == 'completed' for item in items),
                'total': sum(item['status'] != 'superseded' for item in items), 'current': current, 'items': items}

    def _goal(self, run):
        request = run.get('_request', {})
        content = request.get('content', '')
        materialized = request.get('as_requirement', False)
        if content and not materialized:
            # Compare inside SQLite so checking for a materialized goal does not
            # fetch other sources' raw bodies into a task working on one source.
            with self.store.lock:
                for source_id in run.get('_source_ids', []):
                    match = self.store.db.execute(
                        "SELECT 1 FROM objects WHERE kind='source' AND id=? AND project_id=? "
                        "AND json_extract(payload, '$._text')=?",
                        (source_id, run['project_id'], content)).fetchone()
                    if match:
                        materialized = True
                        break
        if materialized:
            return '根据已提供需求完成当前任务：' + run.get('intent', request.get('intent', ''))
        return content

    def model_context(self, run: dict, task: str, data: dict, budget: int = 16000) -> dict:
        """Copy caller-selected business inputs and only the relevant settings.

        Values and rules are never shortened. An oversized context must instead
        be split by its caller or represented as evidence before another call.
        """
        _budget(budget)
        if not isinstance(data, dict):
            raise DomainError('模型上下文必须为对象')
        task = task.removeprefix('work_')
        group = _TASK_GROUPS.get(task, task)
        profile = run.get('_profile', {})
        selected_profile = {field: copy.deepcopy(profile[field])
                            for field in _PROFILE_FIELDS.get(group, ()) if field in profile}
        context = copy.deepcopy(data)
        if 'goal' not in context:
            context['goal'] = self._goal(run)
        context.setdefault('depth', run.get('agent', {}).get('depth',
                           run.get('_request', {}).get('depth', 'standard')))
        # The profile belongs to the run. Explicit business values can use any
        # other key; a profile override would bypass task-specific selection.
        if 'profile' in context and context['profile'] != selected_profile:
            raise DomainError('profile 为任务配置保留字段；请将业务数据放入单独字段')
        context['profile'] = selected_profile
        required = _size(context)
        if required > budget:
            raise DomainError(f'当前任务上下文需要 {required} 字符，超过 {budget} 字符预算；'
                              '请拆分证据或工作项，将长需求保存为来源，或提高上下文预算；规则及业务数据未截断')
        return context
