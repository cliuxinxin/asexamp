"""One conservative admission policy for planning and the final wire prompt."""
import hashlib
import json
import math
from pathlib import Path

from .environment import runtime_value
from .schemas import DomainError

DEFAULT_WINDOW = 32768
DEFAULT_OUTPUT = 8192
MARGIN = 512


def token_estimate(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    ascii_count = sum(ord(c) < 128 for c in text)
    return math.ceil(ascii_count / 3) + (len(text) - ascii_count) * 2


def request_messages(task, context):
    from .model import SYSTEM, TASK_INSTRUCTIONS
    return [
        {'role': 'system', 'content': [{'type': 'text', 'text': SYSTEM + '\nTASK CONTRACT:\n' + TASK_INSTRUCTIONS.get(task, '')}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': json.dumps(context, ensure_ascii=False)}]},
    ]


def validate_capacity(settings):
    result = dict(settings)
    try:
        for key, default in (('context_window', DEFAULT_WINDOW), ('output_tokens', DEFAULT_OUTPUT)):
            value = result.get(key, default)
            if isinstance(value, bool) or str(value) != str(int(value)):
                raise ValueError()
            result[key] = int(value)
        if result['context_window'] == 0:
            result['context_window'] = DEFAULT_WINDOW
        mode = result.get('output_limit_mode', 'request')
        if mode not in ('request', 'server'):
            raise ValueError()
        result['output_limit_mode'] = mode
        allowance = result['output_tokens']
        if mode == 'server':
            value = result.get('server_output_tokens')
            if isinstance(value, bool) or str(value) != str(int(value)):
                raise ValueError()
            result['server_output_tokens'] = allowance = int(value)
        if result['output_tokens'] < 1024 or allowance < 1024 or result['context_window'] < 4096 or allowance >= result['context_window'] - 1024:
            raise ValueError()
    except (ValueError, TypeError):
        raise DomainError('模型容量配置无效：上下文至少 4096 tokens，输出至少 1024，须保留超过 1024 tokens 输入空间；server 模式须声明服务强制输出上限。') from None
    return result


def capacity_settings(directory, settings=None):
    if settings is None:
        path = Path(directory) / 'settings.json'
        settings = json.loads(path.read_text('utf-8')) if path.exists() else {}
    values = dict(getattr(settings, 'value', settings))
    for name, key, default in (
        ('TCG_MODEL_CONTEXT_TOKENS', 'context_window', DEFAULT_WINDOW),
        ('TCG_OUTPUT_TOKENS', 'output_tokens', DEFAULT_OUTPUT),
        ('TCG_OUTPUT_LIMIT_MODE', 'output_limit_mode', 'request'),
        ('TCG_SERVER_OUTPUT_TOKENS', 'server_output_tokens', None),
    ):
        fallback = values.get(key, default)
        raw = runtime_value(directory, name, '' if fallback is None else fallback)
        values[key] = raw if raw != '' else None
    return validate_capacity(values)


def request_budget(directory, task, context, settings=None):
    config = capacity_settings(directory, settings)
    messages = request_messages(task, context)
    serialized = json.dumps(messages, ensure_ascii=False)
    count = token_estimate(serialized)
    output = config['server_output_tokens'] if config['output_limit_mode'] == 'server' else config['output_tokens']
    digest_input = {'messages': messages, 'invocation': {key: config.get(key) for key in (
        'provider', 'base_url', 'model', 'context_window', 'output_tokens', 'output_limit_mode', 'server_output_tokens')}}
    return {'fits': count + output + MARGIN <= config['context_window'] and len(serialized) <= 500000,
            'window': config['context_window'], 'output_tokens': output, 'input_tokens': count,
            'count_method': 'conservative_estimate', 'margin': MARGIN,
            'output_limit_mode': config['output_limit_mode'],
            'request_digest': hashlib.sha256(json.dumps(digest_input, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()}
