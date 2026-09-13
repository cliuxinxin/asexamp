"""Bounded startup evidence; never read secrets or emit environment values."""
import os
import platform
import ssl
import sys
from importlib import metadata
from pathlib import Path

import certifi


_PACKAGES = ('httpx', 'httpcore', 'langchain', 'langchain-core', 'langchain-openai',
             'langchain-ollama', 'langgraph', 'langgraph-checkpoint',
             'langgraph-checkpoint-sqlite', 'openai', 'ollama', 'certifi')
_ENVIRONMENT_NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                      'SSL_CERT_FILE', 'SSL_CERT_DIR')
_TEXT_LIMIT = 1024


def _probe(operation):
    try:
        return operation()
    except Exception:
        # Diagnostic collection must not break startup or expose exception text.
        return None


def _text(value):
    text = str(value)
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT - 3] + '...'


def _path_status(value, *, directory=False):
    if not value:
        return {'path': None, 'available': False}
    path = Path(value)
    return {'path': _text(path),
            'available': _probe(path.is_dir if directory else path.is_file)}


def _settings_status(settings):
    provider = settings.value.get('provider')
    return {'configured': bool(settings.configured()),
            'provider': provider if provider in ('openai', 'ollama') else 'unknown'}


def runtime_snapshot(data_dir, settings):
    """Describe installed runtime and application transport defaults without I/O requests.

    OpenSSL's resolved cafile/capath may contain environment variable values.
    Only its compiled defaults are reported; HTTPX with trust_env=False uses
    certifi's bundle instead. No CA contents or model/header/auth text is read.
    """
    default_paths = _probe(ssl.get_default_verify_paths)
    return {
        'python': {'version': _probe(lambda: _text(platform.python_version())),
                   'executable': _probe(lambda: _text(sys.executable))},
        'platform': {name: _probe(lambda name=name: _text(getattr(platform, name)()))
                     for name in ('system', 'release', 'machine')},
        'packages': {name: _probe(lambda name=name: _text(metadata.version(name)))
                     for name in _PACKAGES},
        'paths': {'cwd': _probe(lambda: _text(Path.cwd())),
                  'data_dir': _probe(lambda: _text(Path(data_dir).expanduser().resolve()))},
        'environment_present': {name: name in os.environ for name in _ENVIRONMENT_NAMES},
        'settings': _probe(lambda: _settings_status(settings)),
        'tls': {
            'openssl_version': _probe(lambda: _text(ssl.OPENSSL_VERSION)),
            'openssl_default_cafile': _probe(lambda: _path_status(default_paths.openssl_cafile)),
            'openssl_default_capath': _probe(lambda: _path_status(default_paths.openssl_capath, directory=True)),
            'httpx_default_cafile': _probe(lambda: _path_status(certifi.where())),
        },
        'transport': {
            'configuration_source': 'application_default',
            'proxy_mode': 'direct', 'trust_env': False, 'follow_redirects': False,
            'tls_verify': True, 'ca_source': 'certifi',
            'environment_proxy_used': False, 'environment_ca_used': False,
        },
    }


def record_runtime(diagnostics, data_dir, settings):
    """Startup logging is best effort, including failures in the logging sink."""
    try:
        diagnostics.record('service.runtime', **runtime_snapshot(data_dir, settings))
    except Exception:
        pass
