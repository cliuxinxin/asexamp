"""Prompt files remain plain text and diagnostics preserve exact loaded versions."""
import hashlib

import pytest

from tcg.prompt_loader import load_prompt, prompt_templates
from tcg.schemas import DomainError


def write_catalog(directory, content):
    path = directory / 'testing.yaml'
    path.write_text(content, encoding='utf-8')
    return path


def test_prompt_reload_preserves_literals_and_exact_version_metadata(tmp_path):
    body = 'version: 1\nprompts:\n  example: |-\n    {"refs": ["src#P1"]}\n    {{ untouched }} ${HOME}\n    flowchart TD\n      A["理解"] --> B["场景"]\n'
    path = write_catalog(tmp_path, body)
    first = load_prompt('testing.example', directory=tmp_path)
    assert first == '{"refs": ["src#P1"]}\n{{ untouched }} ${HOME}\nflowchart TD\n  A["理解"] --> B["场景"]'
    composed = 'prefix ' + first + '\nsuffix'
    metadata = prompt_templates(composed)
    assert metadata == [{'file': 'testing.yaml', 'key': 'example', 'version': 1,
                         'sha256': hashlib.sha256(first.encode()).hexdigest()}]
    path.write_text(body.replace('理解', '业务理解'), encoding='utf-8')
    second = load_prompt('testing.example', directory=tmp_path)
    assert '业务理解' in second
    assert prompt_templates(first) == metadata
    assert prompt_templates(second)[0]['sha256'] != metadata[0]['sha256']
    assert len(prompt_templates(first + first)) == 1


@pytest.mark.parametrize('body, expected', [
    ('version: 1\nprompts:\n  example: 42\n', 'example'),
    ('version: 1\nprompts:\n  example: ""\n', 'example'),
    ('version: 2\nprompts:\n  example: okay\n', 'version'),
    ('version: 1\nprompts:\n  example: first\n  example: second\n', '重复'),
    ('version: 1\nprompts:\n  example: [\n', 'YAML'),
    ('!!python/object/apply:os.system ["touch /tmp/tcg-should-never-execute"]', 'YAML'),
    ('version: 1\nprompts:\n  missing: okay\n', 'example'),
])
def test_invalid_prompt_catalog_fails_with_file_and_field(tmp_path, body, expected):
    write_catalog(tmp_path, body)
    with pytest.raises(DomainError) as raised:
        load_prompt('testing.example', directory=tmp_path)
    assert 'testing.yaml' in str(raised.value)
    assert expected in str(raised.value)
    assert raised.value.category == 'prompt_configuration'


def test_missing_and_path_traversal_are_rejected(tmp_path):
    with pytest.raises(DomainError, match='missing.yaml'):
        load_prompt('missing.example', directory=tmp_path)
    with pytest.raises(DomainError):
        load_prompt('../testing.example', directory=tmp_path)


def test_oversized_catalog_is_rejected(tmp_path):
    write_catalog(tmp_path, 'version: 1\nprompts:\n  example: ' + 'a' * 150_000)
    with pytest.raises(DomainError, match='大小'):
        load_prompt('testing.example', directory=tmp_path)


def test_shipped_catalogs_are_nonempty_and_match_active_prompt_contracts():
    assert 'Speak Chinese' in load_prompt('chat.system')
    assert 'supplied native function' in ' '.join(load_prompt('business.policy').split())
    assert 'Use the bound tool' in load_prompt('native.system')
    assert 'exact IDs only from evidence' in ' '.join(load_prompt('evidence.repair').split())
    assert 'stateDiagram-v2' in load_prompt('analysis.diagrams')


async def test_native_request_logs_prompt_versions_without_sending_metadata(tmp_path):
    import json
    import httpx
    from tcg.diagnostics import Diagnostics
    from tcg.model import LangChainGateway, Settings
    from tcg.storage import Store

    store = Store(tmp_path)
    diagnostics = Diagnostics(store)
    snapshots, wire = [], []
    settings = Settings(tmp_path)
    settings.save({'provider': 'openai', 'base_url': 'http://model/v1', 'model': 'local',
                   'timeout_seconds': 5, 'api_key': 'private-token'})
    def handler(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'submit1', 'type': 'function',
            'function': {'name': 'submit_check_prompt', 'arguments': '{"title":"okay"}'}}]}}]})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(settings, http_client=client)
            gateway.diagnostics = diagnostics
            gateway.request_recorder = snapshots.append
            await gateway.generate_native('check_prompt', {'requirement': 'private-requirement'},
                {'type': 'object', 'properties': {'title': {'type': 'string'}}, 'required': ['title']},
                load_prompt('business.policy') + load_prompt('business.understand'))
        snapshot = snapshots[0]
        sources = snapshot['prompt_templates']
        assert [(source['file'], source['key']) for source in sources] == [
            ('native.yaml', 'system'), ('business.yaml', 'policy'), ('business.yaml', 'understand')]
        content = wire[0]['messages'][0]['content']
        assert snapshot['system_prompt_sha256'] == hashlib.sha256(
            json.dumps([content], ensure_ascii=False).encode()).hexdigest()
        assert 'tcg_prompt_templates' not in json.dumps(wire)
        events = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        logged = next(event for event in events if event['event'] == 'model.transport_start')
        assert logged['prompt_templates'] == sources
        assert logged['system_prompt_sha256'] == snapshot['system_prompt_sha256']
        assert 'private-requirement' not in json.dumps(events)
        assert 'private-token' not in json.dumps(events)
    finally:
        diagnostics.close()
        store.close()
