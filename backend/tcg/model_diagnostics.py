"""Safe connection metadata and user-facing troubleshooting, without payload text."""
import re
import hashlib
import socket
import ssl

import httpx

from .diagnostics import endpoint_origin, error_details
from .storage import uid


HINTS = {
    'dns': ['在启动 TCG 的同一台机器检查模型域名解析和 VPN / 内网 DNS。'],
    'connection': ['检查模型服务是否启动、主机端口及防火墙；容器里的 127.0.0.1 指向容器自身。'],
    'tls': ['检查模型证书的域名、有效期和 Python 信任的 CA；浏览器信任证书不代表 Python 也信任。'],
    'proxy': ['检查网络代理；当前内置客户端直接连接，不读取系统 PAC 或 HTTP_PROXY / HTTPS_PROXY。'],
    'timeout': ['查看日志中的 timeout_phase 和 timeout_seconds，区分连接超时与等待模型响应超时。'],
    'authentication': ['核对 API Key、自定义认证头和网关要求；日志只记录认证头名称，不记录值。'],
    'configuration': ['对照日志中的最终 endpoint、模型名称与服务文档，核对路径和工具调用支持。'],
    'rate_limit': ['检查服务配额、并发限制与上游限流日志，稍后手动重试。'],
    'service_unavailable': ['按 provider_request_id 和 HTTP 状态检查网关、模型服务的上游日志。'],
    'tool_calling': ['检查模型与网关是否支持并转发 tools、tool_choice、tool_calls。'],
    'protocol': ['检查 content_type 和 HTTP 状态；服务必须返回模型协议，不能返回登录页或代理 HTML 页面。'],
    'transport': ['检查模型地址和网络可达性，并在服务日志中查找该请求编号的底层异常。'],
}

PROVIDER_CODES = frozenset({'upstream_failure', 'server_error', 'internal_error', 'internal_server_error',
    'invalid_api_key', 'authentication_error', 'permission_denied', 'invalid_request_error',
    'model_not_found', 'deployment_not_found', 'insufficient_quota', 'rate_limit_exceeded',
    'rate_limit_error', 'context_length_exceeded', 'max_tokens', 'unsupported_parameter',
    'not_found', 'bad_request', 'service_unavailable', 'overloaded_error', 'api_error'})


def _secrets(headers):
    values = {str(v) for v in headers.values() if v}
    for value in list(values):
        if value.lower().startswith(('bearer ', 'basic ')):
            values.add(value.split(' ', 1)[1])
    return values


def _token(value, headers):
    value = str(value) if isinstance(value, (str, int)) else ''
    if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', value):
        return None
    return None if any(secret in value for secret in _secrets(headers)) else value


def connection_details(settings, headers, endpoint, injected_client=False):
    # Settings validates URLs without credentials/query. Still redact path secrets.
    for secret in sorted(_secrets(headers), key=len, reverse=True):
        endpoint = endpoint.replace(secret, '[redacted]')
    return {'endpoint': endpoint, 'endpoint_origin': endpoint_origin(endpoint),
        'provider': settings.get('provider'), 'model': _token(settings.get('model'), headers),
        'timeout_seconds': settings.get('timeout_seconds'), 'auth_mode': settings.get('auth_mode', 'bearer'),
        'auth_header_names': sorted(headers, key=str.lower),
        'client_source': 'injected' if injected_client else 'application',
        'trust_env': None if injected_client else False,
        'proxy_mode': 'injected_client' if injected_client else 'direct',
        'tls_verify': None if injected_client else True,
        'follow_redirects': None if injected_client else False}


def response_details(response, headers, body=None):
    result = {'http_status': response.status_code, 'response_bytes': len(response.content),
        'content_type': _token(response.headers.get('content-type', '').split(';')[0], headers)}
    for name in ('x-request-id', 'request-id', 'x-correlation-id', 'x-ms-request-id'):
        value = _token(response.headers.get(name), headers)
        if value:
            result['provider_request_id'] = value
            break
    error = body.get('error') if isinstance(body, dict) else None
    if isinstance(error, dict):
        for field in ('code', 'type'):
            value = _token(error.get(field), headers)
            if value in PROVIDER_CODES:
                result['provider_error_' + field] = value
            elif value:
                result['provider_error_' + field + '_sha256'] = hashlib.sha256(value.encode()).hexdigest()
    # Provider free text may contain credentials or reflected requirements.
    return result


def transport_category(exc):
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(e, socket.gaierror) for e in chain):
        return 'dns', '模型域名解析失败；请检查启动环境的 DNS 与内网连接。'
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return 'tls', '模型 TLS / 证书校验失败；请检查启动环境的证书信任配置。'
    if any(isinstance(e, httpx.ProxyError) for e in chain):
        return 'proxy', '模型代理连接失败；请检查代理与网络配置。'
    if any(isinstance(e, (httpx.TimeoutException, TimeoutError)) for e in chain):
        return 'timeout', '模型请求超时；请检查连接和模型响应耗时。'
    if any(isinstance(e, (httpx.ConnectError, ConnectionError)) for e in chain):
        return 'connection', '无法连接模型服务；请检查模型地址、端口和服务运行状态。'
    return 'transport', '模型请求失败；请按诊断编号检查服务日志后重试。'


def exception_details(exc):
    fields = error_details(exc)
    current, seen = exc, set()
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        for name in ('errno', 'winerror', 'verify_code'):
            value = getattr(current, name, None)
            if isinstance(value, int):
                fields[name] = value
        if isinstance(current, httpx.TimeoutException):
            fields['timeout_phase'] = type(current).__name__.removesuffix('Timeout').lower()
        elif isinstance(current, TimeoutError):
            fields.setdefault('timeout_phase', 'overall')
        current = current.__cause__ or current.__context__
    return fields


def failure_part(exc):
    category = getattr(exc, 'category', 'application')
    reference = getattr(exc, 'call_id', None) or uid('diag_')
    fields = getattr(exc, 'model_diagnostic', {})
    return {'type': 'diagnostic', 'reference_id': reference, 'call_id': getattr(exc, 'call_id', None),
        'category': category, 'message': '可按诊断编号在服务日志中定位本次失败。',
        'http_status': fields.get('http_status'),
        'hints': HINTS.get(category, ['查看该诊断编号对应的异常类别和代码位置。']),
        'log_path': 'logs/tcg.log'}
