"""Startup evidence is useful without disclosing configuration secrets."""
import json
import ssl
from pathlib import Path

import certifi
import pytest

from tcg import runtime_diagnostics
from tcg.main import create_app
from tcg.model import Settings


def test_runtime_snapshot_reports_defaults_without_environment_values(tmp_path, monkeypatch):
    secrets = []
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                 'SSL_CERT_FILE', 'SSL_CERT_DIR'):
        value = 'SENSITIVE-' + name
        secrets.append(value)
        monkeypatch.setenv(name, value)
    ca_file = tmp_path / 'default-ca.pem'
    ca_file.write_text('certificate contents must not be read')
    monkeypatch.setattr(ssl, 'get_default_verify_paths', lambda: ssl.DefaultVerifyPaths(
        'SENSITIVE-SSL_CERT_FILE', 'SENSITIVE-SSL_CERT_DIR', 'SSL_CERT_FILE',
        str(ca_file), 'SSL_CERT_DIR', str(tmp_path)))
    monkeypatch.setattr(certifi, 'where', lambda: str(ca_file))
    settings = Settings(tmp_path)
    settings.value.update(provider='openai', model='SENSITIVE-MODEL',
                          base_url='https://SENSITIVE-ENDPOINT', _api_key='SENSITIVE-KEY')
    settings.secret = lambda: pytest.fail('Startup must not decrypt credentials')
    settings.headers = lambda: pytest.fail('Startup must not inspect headers')

    payload = runtime_diagnostics.runtime_snapshot(tmp_path / '..' / tmp_path.name, settings)
    encoded = json.dumps(payload)
    assert not any(value in encoded for value in [*secrets, 'SENSITIVE-MODEL',
                                                'SENSITIVE-ENDPOINT', 'SENSITIVE-KEY'])
    assert payload['python']['version']
    assert payload['python']['executable']
    assert payload['platform']['system']
    assert payload['paths'] == {'cwd': str(Path.cwd()), 'data_dir': str(tmp_path.resolve())}
    assert payload['packages']['httpx']
    assert payload['packages']['langchain']
    assert payload['packages']['langgraph']
    assert all(payload['environment_present'].values())
    assert payload['settings'] == {'configured': True, 'provider': 'openai'}
    assert payload['tls']['openssl_version'] == ssl.OPENSSL_VERSION
    assert payload['tls']['openssl_default_cafile'] == {'path': str(ca_file), 'available': True}
    assert payload['tls']['openssl_default_capath'] == {'path': str(tmp_path), 'available': True}
    assert payload['tls']['httpx_default_cafile'] == {'path': str(ca_file), 'available': True}
    assert payload['transport'] == {'configuration_source': 'application_default',
        'proxy_mode': 'direct', 'trust_env': False, 'follow_redirects': False,
        'tls_verify': True, 'ca_source': 'certifi',
        'environment_proxy_used': False, 'environment_ca_used': False}


def test_runtime_snapshot_handles_missing_metadata_and_probe_errors(tmp_path, monkeypatch):
    def unavailable(*args):
        raise RuntimeError('SENSITIVE-PROBE-ERROR')

    settings = Settings(tmp_path)
    settings.value['provider'] = 'SENSITIVE-PROVIDER'
    monkeypatch.setattr(runtime_diagnostics.metadata, 'version', unavailable)
    monkeypatch.setattr(ssl, 'get_default_verify_paths', unavailable)
    monkeypatch.setattr(certifi, 'where', unavailable)
    monkeypatch.setattr(runtime_diagnostics.sys, 'executable', 'x' * 4000)
    monkeypatch.delenv('HTTP_PROXY', raising=False)

    payload = runtime_diagnostics.runtime_snapshot(tmp_path, settings)
    assert all(value is None for value in payload['packages'].values())
    assert payload['environment_present']['HTTP_PROXY'] is False
    assert payload['tls']['openssl_default_cafile'] is None
    assert payload['tls']['httpx_default_cafile'] is None
    assert payload['settings'] == {'configured': False, 'provider': 'unknown'}
    assert len(payload['python']['executable']) <= 1024
    assert 'SENSITIVE' not in json.dumps(payload)


async def test_startup_emits_one_runtime_event(tmp_path):
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        assert app.state.engine is not None
    events = [json.loads(line) for line in (tmp_path / 'logs' / 'tcg.log').read_text().splitlines()]
    runtime = [event for event in events if event['event'] == 'service.runtime']
    assert len(runtime) == 1
    assert runtime[0]['paths']['data_dir'] == str(tmp_path.resolve())


async def test_runtime_logging_failure_does_not_block_startup(tmp_path, monkeypatch):
    from tcg.diagnostics import Diagnostics

    events = []

    def broken_record(self, event, **fields):
        events.append(event)
        raise RuntimeError('SENSITIVE-LOGGING-ERROR')

    monkeypatch.setattr(Diagnostics, 'record', broken_record)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        assert app.state.engine is not None
    assert events.count('service.runtime') == 1
