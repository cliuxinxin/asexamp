"""Regression coverage for durable, evidence-aware validation recovery."""
import copy

from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.schemas import DomainError
from test_backend_api import setup_chat, start, until
from test_incremental_agent import WorkModel


class InvalidReferences(WorkModel):
    def __init__(self, stall=False):
        super().__init__()
        self.stall = stall
        self.references = {}

    async def generate(self, task, context):
        if task == 'agent_repair_batch':
            self.calls.append((task, copy.deepcopy(context)))
            return {'repairs': [{'path': f['path'], 'value': f['value'] if self.stall else self.references[f['path']]}
                                for f in context['repairs']]}
        if task == 'agent_repair':
            self.calls.append((task, copy.deepcopy(context)))
            f = context['repair']
            return {'path': f['path'], 'value': f['value'] if self.stall else self.references[f['path']]}
        value = await super().generate(task, context)
        if task == 'work_analyze' and not context.get('validation_recovery'):
            for index, item in enumerate(value['items']):
                self.references[f'items[{index}].refs[0]'] = item['refs'][0]
                item['refs'] = [f'invented-{index}']
        return value


def test_three_invalid_references_are_fixed_together_and_generation_finishes(tmp_path):
    model = InvalidReferences()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='用户名必须填写。\n\n密码最少六位。\n\n失败四次锁定三十分钟。')
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error')
        repairs = [c for t, c in model.calls if t == 'agent_repair_batch']
        assert len(repairs) == 1
        assert len(repairs[0]['repairs']) == 3
        assert all(f['subject']['description'] for f in repairs[0]['repairs'])
        assert all(e.get('text') for e in repairs[0]['evidence'])
        assert 'items' not in repairs[0]
        assert model.analysis_count == 1
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert len(artifact['items']) == 3
        assert all('invented' not in ref for i in artifact['items'] for ref in i['refs'])


def test_stalled_repairs_rederive_only_current_unit_then_continue(tmp_path):
    model = InvalidReferences(stall=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='用户名必须填写。\n\n密码最少六位。\n\n失败四次锁定三十分钟。')
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error')
        recovery = [c for t, c in model.calls if t == 'work_analyze' and c.get('validation_recovery')]
        assert len(recovery) == 1
        assert recovery[0]['validation_recovery']['errors']
        assert len([t for t, _ in model.calls if t in ('agent_repair', 'agent_repair_batch')]) == 2


class InterruptedRepair(WorkModel):
    def __init__(self):
        super().__init__()
        self.offline = True

    async def generate(self, task, context):
        if task == 'agent_repair':
            self.calls.append((task, copy.deepcopy(context)))
            path = context['repair']['path']
            if path.endswith('preconditions') and self.offline:
                error = DomainError('Controlled provider interruption')
                error.retryable = False
                raise error
            return {'path': path, 'value': 'P1' if path.endswith('priority') else '已准备账户'}
        value = await super().generate(task, context)
        if task == 'work_cases':
            value['items'][0].update(preconditions=['invalid'], priority=None)
        return value


def test_retry_after_restart_keeps_successfully_repaired_fields(tmp_path):
    model = InterruptedRepair()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed'
    model.offline = False
    with TestClient(create_app(tmp_path, model)) as client:
        client.post('/api/runs/' + run['id'] + '/retry').raise_for_status()
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error')
        repairs = [c['repair']['path'] for t, c in model.calls if t == 'agent_repair']
        assert repairs.count('items[0].priority') == 1
        assert len([t for t, c in model.calls if t == 'work_cases']) == 1
        assert model.analysis_count == 1

        insights = run['agent']['insights']
        assert any('接续' in i['summary'] for i in insights)


def test_fallback_can_request_evidence_then_finish(tmp_path):
    class NeedsRead(InvalidReferences):
        async def generate(self, task, context):
            if task == 'work_analyze' and context.get('validation_recovery'):
                self.calls.append((task, copy.deepcopy(context)))
                return {'kind': 'need_context', 'requests': [{'tool': 'read_evidence', 'refs': [context['evidence'][0]['id']]}], 'summary': '核对出错规则的原文'}
            if task == 'work_analyze' and context.get('observations'):
                return await WorkModel.generate(self, task, context)
            return await super().generate(task, context)
    model = NeedsRead(stall=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error')
        assert any(t == 'work_analyze' and c.get('observations') for t, c in model.calls)


def test_unrepairable_output_stops_with_progress_without_publishing(tmp_path):
    class AlwaysInvalid(InvalidReferences):
        async def generate(self, task, context):
            if task == 'work_analyze':
                context = {k: v for k, v in context.items() if k != 'validation_recovery'}
            return await super().generate(task, context)
    model = AlwaysInvalid(stall=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed'
        assert not run.get('artifact_ids')
        assert len([t for t, _ in model.calls if t in ('agent_repair', 'agent_repair_batch')]) <= 4
        assert '进度已保存' in run['error']
        assert any('自动修复后仍' in i['summary'] for i in run['agent']['insights'])


def test_reference_batch_rejects_unrequested_business_changes():
    from tcg.incremental_repair import apply_batch, reference_fragments
    import pytest
    from tcg.schemas import OutputValidationError
    original = {'items': [{'title': '必须校验密码', 'refs': ['wrong']}], 'nodes': []}
    fragments = reference_fragments(original, {'source#P1': {'role': 'primary'}})
    with pytest.raises(OutputValidationError):
        apply_batch(original, fragments, {'repairs': [{'path': 'items[0].title', 'value': '跳过校验'}]})
    assert original['items'][0]['title'] == '必须校验密码'
    with pytest.raises(OutputValidationError):
        apply_batch(original, fragments, {'repairs': [{'path': fragments[0]['path'], 'value': 'source#P1'}, None]})
    with pytest.raises(OutputValidationError):
        apply_batch(original, fragments, {'repairs': [{'path': fragments[0]['path'], 'value': 'source#P1'}] * 2})


def test_reference_repair_context_is_bounded_and_marks_excerpt_omissions():
    from tcg.incremental_repair import evidence_snippets
    import json
    refs = {f'source#P{i}': {'id': f'source#P{i}', 'role': 'primary', 'text': '业务原文' * 10000,
                           'excerpt': {'start': 900, 'end': 40900, 'total': 80000}} for i in range(15)}
    snippets = evidence_snippets(refs)
    assert len(json.dumps(snippets, ensure_ascii=False)) <= 5500
    assert len(snippets) == 15
    assert all(e['truncated'] for e in snippets)
    assert snippets[0]['excerpt']['start'] == 900
    assert snippets[0]['excerpt']['end'] == 900 + len(snippets[0]['text'])
