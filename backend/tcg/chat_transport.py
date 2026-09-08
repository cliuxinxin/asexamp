"""Minimal Chat Completions wire format for limited internal gateways.

Keep LangChain message types while avoiding SDK-added request parameters. The
caller owns the HTTP client, timeout, request inspection and SSE delivery.
"""
from langchain_core.messages import AIMessage

from .schemas import DomainError


def chat_completions_endpoint(address):
    address = address.rstrip('/')
    return address if address.endswith('/chat/completions') else address + '/chat/completions'


async def minimal_completion(client, endpoint, body, headers):
    response = await client.post(endpoint, json=body, headers=headers)
    response.raise_for_status()
    data = response.json()
    choices = data.get('choices') if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise DomainError('模型服务未返回有效的 choices[0].message，请检查网关响应格式')
    choice = choices[0]
    message = choice.get('message')
    if not isinstance(message, dict) or not isinstance(message.get('content'), (str, list)):
        raise DomainError('模型服务未返回有效的 assistant content，请检查网关响应格式')
    usage = data.get('usage') or {}
    usage_metadata = None
    if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
        usage_metadata = {'input_tokens': usage['prompt_tokens'], 'output_tokens': usage['completion_tokens'], 'total_tokens': usage['total_tokens']}
    return AIMessage(content=message['content'], response_metadata={'finish_reason': choice.get('finish_reason')}, usage_metadata=usage_metadata)
