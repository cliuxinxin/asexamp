"""Persisted model configuration and the native tool-calling gateway facade.

The former prompt-JSON execution path is retired. Business nodes submit typed
results through generate_native; chat agents use chat_model().bind_tools(...).
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from .context_budget import capacity_settings, validate_capacity
from .environment import model_environment
from .schemas import DomainError

# Compatibility imports for old diagnostic helpers, never execution contracts.
SYSTEM = 'You are TCG Case Agent, an evidence-grounded test-design assistant.'
TASK_INSTRUCTIONS = {}

MODEL_TIMEOUT_SECONDS = 300
MAX_MODEL_TIMEOUT_SECONDS = 3600
DEFAULT_SETTINGS = {'provider': 'ollama', 'base_url': 'http://127.0.0.1:11434', 'model': '', 'timeout_seconds': MODEL_TIMEOUT_SECONDS, 'context_window': 0, 'output_tokens': 8192, 'output_limit_mode': 'request', 'server_output_tokens': None}


def validate_headers(headers, auth_mode='bearer'):
    if auth_mode not in ('bearer', 'headers'):
        raise DomainError('认证模式必须为 bearer 或 headers')
    if not isinstance(headers, dict) or len(headers) > 32:
        raise DomainError('自定义请求头必须为最多 32 项的 JSON 对象')
    seen = set()
    managed = {'host', 'content-length', 'transfer-encoding', 'connection', 'accept', 'content-type', 'accept-encoding', 'user-agent', 'cookie', 'proxy-authorization', 'te', 'trailer', 'upgrade', 'keep-alive', 'proxy-connection', 'expect'}
    for name, value in headers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", name):
            raise DomainError('请求头名称无效；请使用标准 HTTP token 名称')
        if name.lower() in managed or name.lower() in seen:
            raise DomainError('不能覆盖托管传输请求头或使用大小写重复的名称')
        seen.add(name.lower())
        if not isinstance(value, str) or len(value) > 8000 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise DomainError('请求头值必须为单行 ASCII 文本，不能包含换行或控制字符')
    if 'authorization' in seen and auth_mode != 'headers':
        raise DomainError('显式 Authorization 需要选择 headers 认证模式，避免与 Bearer API Key 冲突')
    return dict(headers)

class Settings:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'settings.json'
        self.key_path = self.directory / '.secret.key'
        self.value = dict(DEFAULT_SETTINGS)
        self.value['auth_mode'] = 'bearer'
        if self.path.exists():
            self.value.update(json.loads(self.path.read_text('utf-8')))
        self.env_file, layers = model_environment(self.directory)
        self.environment_managed = any(layers)
        self._environment_key = None
        self._environment_headers = None
        for layer in layers:
            if not layer:
                continue
            previous_endpoint = (self.value['provider'], self.value['base_url'])
            candidate = {**self.value, **{key: value for key, value in layer.items() if key not in ('api_key', 'headers_json')}}
            try:
                candidate['timeout_seconds'] = int(candidate['timeout_seconds'])
            except (TypeError, ValueError):
                raise DomainError('TCG_MODEL_TIMEOUT_SECONDS 必须为 5–3600 的整数') from None
            if not 5 <= candidate['timeout_seconds'] <= MAX_MODEL_TIMEOUT_SECONDS:
                raise DomainError('TCG_MODEL_TIMEOUT_SECONDS 必须为 5–3600 的整数')
            if candidate['provider'] not in ('openai', 'ollama'):
                raise DomainError('TCG_MODEL_PROVIDER 必须为 openai 或 ollama')
            self.validate_address(candidate['base_url'])
            candidate['base_url'] = candidate['base_url'].rstrip('/')
            candidate['model'] = candidate['model'].strip()
            if len(candidate['model']) > 200 or len(layer.get('api_key', '')) > 2000:
                raise DomainError('.env 模型名或 API Key 超过长度限制')
            if (candidate['provider'], candidate['base_url']) != previous_endpoint:
                candidate.pop('_api_key', None)
                candidate.pop('_headers', None)
                self._environment_key = ''
                self._environment_headers = {}
            if 'api_key' in layer:
                self._environment_key = layer['api_key']
            if 'headers_json' in layer:
                try:
                    self._environment_headers = json.loads(layer['headers_json'] or '{}')
                except (ValueError, TypeError):
                    raise DomainError('TCG_MODEL_HEADERS_JSON 必须为有效 JSON 对象，且不能跨行') from None
            self.value = candidate
            validate_headers(self.headers(), candidate.get('auth_mode', 'bearer'))

        self.value.update(capacity_settings(self.directory, self.value))

    def headers(self):
        if self._environment_headers is not None:
            return dict(self._environment_headers) if isinstance(self._environment_headers, dict) else self._environment_headers
        if not self.value.get('_headers'):
            return {}
        try:
            return json.loads(Fernet(self.key_path.read_bytes()).decrypt(self.value['_headers'].encode()).decode())
        except Exception:
            raise DomainError('无法解密自定义请求头，请重新保存模型设置') from None

    def encrypt(self, text):
        if not self.key_path.exists():
            fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(Fernet.generate_key())
        return Fernet(self.key_path.read_bytes()).encrypt(text.encode()).decode()

    def secret(self):
        if self._environment_key is not None:
            return self._environment_key
        ciphertext = self.value.get('_api_key')
        if not ciphertext:
            return ''
        if not self.key_path.exists():
            raise DomainError('本地密钥文件缺失，请重新保存 API Key')
        try:
            return Fernet(self.key_path.read_bytes()).decrypt(ciphertext.encode()).decode()
        except Exception:
            raise DomainError('无法解密 API Key，请重新保存模型设置') from None

    def public(self):
        has_key = bool(self._environment_key) if self._environment_key is not None else bool(self.value.get('_api_key'))
        return {key: self.value[key] for key in DEFAULT_SETTINGS} | {
            'has_api_key': has_key, 'environment_managed': self.environment_managed,
            'env_file': str(self.env_file) if self.env_file else None,
            'timeout_policy': 'configured_per_attempt',
            'context_policy': 'server',
            'auth_mode': self.value.get('auth_mode', 'bearer'),
            'header_names': sorted(self.headers(), key=str.lower), 'has_headers': bool(self.headers()),
        }

    def configured(self):
        return bool(self.value.get('model', '').strip())

    @staticmethod
    def validate_address(address):
        try:
            parsed = urlparse(address)
            valid = (len(address) <= 1000 and parsed.scheme in ('http', 'https') and parsed.hostname
                     and not (parsed.username or parsed.password or parsed.query or parsed.fragment))
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise DomainError('模型地址必须为有效 HTTP(S) URL，不能包含凭据、查询参数或片段')

    def save(self, request):
        if self.environment_managed:
            raise DomainError('模型连接由 .env 或环境变量管理，请修改配置文件并重启服务', 409)
        self.validate_address(request['base_url'])
        result = {key: request.get(key, self.value.get(key, default)) for key, default in DEFAULT_SETTINGS.items()}
        result = validate_capacity(result)
        result['auth_mode'] = request.get('auth_mode', 'bearer')
        try:
            result['timeout_seconds'] = int(result['timeout_seconds'])
        except (TypeError, ValueError):
            raise DomainError('模型超时必须为 5–3600 秒的整数') from None
        if not 5 <= result['timeout_seconds'] <= MAX_MODEL_TIMEOUT_SECONDS:
            raise DomainError('模型超时必须为 5–3600 秒的整数')
        result['base_url'] = result['base_url'].rstrip('/')
        result['model'] = result['model'].strip()
        same_endpoint = all(result[key] == self.value[key] for key in ('provider', 'base_url'))
        headers = request.get('headers')
        if request.get('clear_headers'):
            headers = {}
        elif headers is None:
            headers = self.headers() if same_endpoint else {}
        headers = validate_headers(headers, result['auth_mode'])
        if headers:
            result['_headers'] = self.encrypt(json.dumps(headers))
        api_key = request.get('api_key')
        if request.get('clear_api_key'):
            api_key = ''
        elif api_key is None and same_endpoint:
            result['_api_key'] = self.value.get('_api_key')
        if api_key:
            if not self.key_path.exists():
                fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(Fernet.generate_key())
            result['_api_key'] = Fernet(self.key_path.read_bytes()).encrypt(api_key.encode()).decode()
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False)
        os.replace(temporary, self.path)
        self.value = result
        return self.public()


class LangChainGateway:
    def __init__(self, settings, http_client=None):
        self.settings = settings
        self.diagnostics = None
        self.request_recorder = None
        self._http_client = http_client
        self._owns_http_client = http_client is None

    async def close(self):
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def chat_model(self):
        """Return the configured LangChain model with native bind_tools support."""
        from .native_model import chat_model
        return chat_model(self)

    async def generate_native(self, task, context, schema, instruction):
        """Submit a business result through one schema-bound native tool call."""
        from .native_model import generate_native
        return await generate_native(self, task, context, schema, instruction)

    async def generate(self, task, context):
        """Reject retired prompt-JSON callers instead of silently reviving them."""
        error = DomainError('旧模型调用入口已停用；请通过原生 Tool Calling 接口执行当前任务。')
        error.category, error.retryable = 'deprecated_model_api', False
        raise error

    async def generate_stream(self, task, context, on_text):
        return await self.generate(task, context)

    @staticmethod
    def visible_text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return ''.join(part.get('text', '') for part in content
                           if isinstance(part, dict) and part.get('type') in ('text', 'output_text')
                           and isinstance(part.get('text'), str))
        return ''

    async def test(self):
        result = await self.generate_native('connection_test', {},
            {'type': 'object', 'properties': {'ok': {'type': 'boolean', 'const': True}},
             'required': ['ok'], 'additionalProperties': False},
            'Confirm this native tool-calling connection by submitting ok=true.')
        if result.get('ok') is not True:
            raise DomainError('服务可访问，但模型未通过原生 Tool Calling 测试；请选择支持工具调用的模型。')
