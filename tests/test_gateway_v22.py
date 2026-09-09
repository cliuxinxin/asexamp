import asyncio
import json

import httpx
import pytest

from tcg.model import LangChainGateway, Settings, TASK_INSTRUCTIONS
from tcg.schemas import DomainError


def configured(tmp_path, base_url="http://10.206.3.151:8000/api/v1", timeout=17):
    settings = Settings(tmp_path)
    settings.save({
        "provider": "openai", "base_url": base_url, "model": "private-model",
        "timeout_seconds": timeout, "api_key": "PRIVATE-GATEWAY-KEY",
    })
    return settings


def response(content='{"ok":true}', finish_reason="stop"):
    return {
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def run_with_transport(settings, handler, callback=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    gateway = LangChainGateway(settings, http_client=client)

    async def exercise():
        try:
            if callback is None:
                return await gateway.generate("connection_test", {})
            return await gateway.generate_stream("connection_test", {}, callback)
        finally:
            await gateway.close()
            await client.aclose()

    return asyncio.run(exercise())


def test_close_does_not_close_injected_client(tmp_path):
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response())))
    gateway = LangChainGateway(configured(tmp_path), http_client=client)
    asyncio.run(gateway.close())
    assert not client.is_closed
    asyncio.run(client.aclose())


def test_close_closes_internally_owned_client(tmp_path):
    gateway = LangChainGateway(configured(tmp_path))

    async def exercise():
        gateway._http_client = httpx.AsyncClient()
        client = gateway._http_client
        await gateway.close()
        return client.is_closed

    assert asyncio.run(exercise()) is True


@pytest.mark.parametrize("task", ["agent_intake", "agent_plan", "agent_analyze",
                                   "agent_scenarios", "agent_cases", "agent_summary"])
def test_reliable_tasks_honor_explicit_output_contract_without_skipping_source_rules(task):
    instruction = TASK_INSTRUCTIONS[task]
    assert "context.output_contract" in instruction
    assert "overrides conflicting legacy examples" in instruction
    assert "Never skip evidence, reference, grounding, or source rules" in instruction


@pytest.mark.parametrize("base_url", [
    "http://10.206.3.151:8000",
    "http://10.206.3.151:8000/api/v1",
    "http://10.206.3.151:8000/api/v1/chat/completions",
])
def test_openai_private_gateway_exact_protocol(tmp_path, base_url):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response())

    settings = configured(tmp_path, base_url)
    assert run_with_transport(settings, handler) == {"ok": True}
    request = captured[0]
    assert str(request.url) == "http://10.206.3.151:8000/api/v1/chat/completions"
    assert request.headers["X-API-Key"] == "PRIVATE-GATEWAY-KEY"
    body = json.loads(request.content)
    assert set(body) == {"model", "messages"}
    assert body["model"] == "private-model"
    assert all(isinstance(message["content"], list) for message in body["messages"])
    assert all(block["type"] == "text" for message in body["messages"] for block in message["content"])


def test_timeout_is_configurable_and_stream_callback_receives_completed_text(tmp_path):
    settings = configured(tmp_path, timeout=17)
    assert settings.value["timeout_seconds"] == 17
    assert settings.public()["timeout_policy"] == "configured_per_attempt"
    pieces = []

    async def callback(text):
        pieces.append(text)

    result = run_with_transport(settings, lambda request: httpx.Response(200, json=response()), callback)
    assert result == {"ok": True}
    assert pieces == ['{"ok":true}']


@pytest.mark.parametrize("status,category", [(401, "authentication"), (403, "authentication"),
                                               (400, "configuration"), (404, "configuration")])
def test_protocol_and_auth_failures_are_nonretryable_and_redacted(tmp_path, status, category):
    diagnostics = []
    gateway_settings = configured(tmp_path)

    def handler(request):
        return httpx.Response(status, json={"error": "PRIVATE-GATEWAY-KEY"})

    gateway = LangChainGateway(gateway_settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    gateway.diagnostics = type("Diagnostics", (), {"record": lambda self, *args, **kwargs: diagnostics.append((args, kwargs))})()

    async def exercise():
        try:
            await gateway.generate("connection_test", {})
        finally:
            await gateway.close()

    with pytest.raises(DomainError) as caught:
        asyncio.run(exercise())
    assert caught.value.retryable is False
    assert caught.value.category == category
    assert "PRIVATE-GATEWAY-KEY" not in str(caught.value)
    assert "PRIVATE-GATEWAY-KEY" not in json.dumps(diagnostics)


@pytest.mark.parametrize("payload", [
    {},
    {"choices": []},
    {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
    response("not json"),
    response("[]"),
    response('{"ok":true}', "length"),
    response('{"ok":true}', None),
    response('{"ok":true}', "content_filter"),
    response('{"ok":true}', "unknown"),
    {"choices": [{"message": {"content": '{"ok":true}'}}]},
])
def test_rejects_empty_invalid_or_truncated_responses(tmp_path, payload):
    with pytest.raises(DomainError):
        run_with_transport(configured(tmp_path), lambda request: httpx.Response(200, json=payload))


def test_rejects_invalid_gateway_envelope_json(tmp_path):
    with pytest.raises(DomainError) as caught:
        run_with_transport(configured(tmp_path), lambda request: httpx.Response(200, content=b"truncated{"))
    assert caught.value.retryable is False
    assert caught.value.category == "protocol"
