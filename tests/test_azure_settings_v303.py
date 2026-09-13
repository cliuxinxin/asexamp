import json

import pytest

from tcg import environment
from tcg.model import Settings
from tcg.schemas import DomainError


AZURE_ENV = {
    'AZURE_OPENAI_API_KEY': 'azure-test-key',
    'AZURE_OPENAI_ENDPOINT': 'resource.openai.azure.com/',
    'AZURE_API_VERSION': '2025-01-01-preview',
    'AZURE_OPENAI_DEPLOYMENT': 'test-deployment',
}


@pytest.fixture(autouse=True)
def isolated_model_environment(tmp_path, monkeypatch):
    for name in (*environment.MODEL_ENV, *AZURE_ENV, 'TCG_ENV_FILE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(environment, 'PROJECT_ROOT', tmp_path)


def write_env(tmp_path, values):
    (tmp_path / '.env').write_text(''.join(f'{key}="{value}"\n' for key, value in values.items()))


def save_legacy(tmp_path):
    settings = Settings(tmp_path)
    settings.save({
        'provider': 'openai', 'base_url': 'https://gateway.example.test/api/v1',
        'model': 'saved-deployment', 'api_key': 'legacy-test-key',
        'headers': {'X-Legacy-Account': 'legacy-test-account'},
    })
    return (tmp_path / 'settings.json').read_bytes()


def test_azure_env_normalizes_resource_and_preserves_saved_connection(tmp_path):
    original = save_legacy(tmp_path)
    write_env(tmp_path, AZURE_ENV)
    settings = Settings(tmp_path)
    assert settings.value['provider'] == 'azure'
    assert settings.value['base_url'] == 'https://resource.openai.azure.com'
    assert settings.value['model'] == 'test-deployment'
    assert settings.value['api_version'] == '2025-01-01-preview'
    assert settings.secret() == 'azure-test-key'
    assert settings.headers() == {}
    assert '_api_key' not in settings.value and '_headers' not in settings.value
    assert settings.public()['environment_managed'] is True
    assert settings.public()['api_version'] == '2025-01-01-preview'
    assert 'test-key' not in json.dumps(settings.public())
    assert (tmp_path / 'settings.json').read_bytes() == original
    (tmp_path / '.env').unlink()
    restored = Settings(tmp_path)
    assert restored.value['provider'] == 'openai'
    assert restored.secret() == 'legacy-test-key'
    assert restored.headers() == {'X-Legacy-Account': 'legacy-test-account'}


def test_file_azure_wins_over_process_legacy_with_no_header_or_key_leak(tmp_path, monkeypatch):
    write_env(tmp_path, {**AZURE_ENV, 'TCG_MODEL_HEADERS_JSON': 'invalid-legacy-json'})
    monkeypatch.setenv('TCG_MODEL_PROVIDER', 'invalid-legacy-provider')
    monkeypatch.setenv('TCG_MODEL_BASE_URL', 'invalid-legacy-endpoint')
    monkeypatch.setenv('TCG_API_KEY', 'legacy-process-test-key')
    monkeypatch.setenv('TCG_MODEL_AUTH_MODE', 'headers')
    monkeypatch.setenv('TCG_MODEL_TIMEOUT_SECONDS', '42')
    settings = Settings(tmp_path)
    assert settings.value['provider'] == 'azure'
    assert settings.value['auth_mode'] == 'bearer'
    assert settings.value['timeout_seconds'] == 42
    assert settings.secret() == 'azure-test-key'
    assert settings.headers() == {}


def test_process_azure_overrides_file_azure_by_field(tmp_path, monkeypatch):
    write_env(tmp_path, AZURE_ENV)
    monkeypatch.setenv('AZURE_OPENAI_ENDPOINT', 'https://private.example.test/')
    monkeypatch.setenv('AZURE_OPENAI_API_KEY', 'process-test-key')
    monkeypatch.setenv('AZURE_OPENAI_DEPLOYMENT', 'process-deployment')
    settings = Settings(tmp_path)
    assert settings.value['base_url'] == 'https://private.example.test'
    assert settings.secret() == 'process-test-key'
    assert settings.value['model'] == 'process-deployment'
    assert settings.value['api_version'] == '2025-01-01-preview'


@pytest.mark.parametrize('model_source', ['saved', 'file', 'process'])
def test_azure_deployment_falls_back_to_configured_model(tmp_path, monkeypatch, model_source):
    save_legacy(tmp_path)
    values = {key: value for key, value in AZURE_ENV.items() if key != 'AZURE_OPENAI_DEPLOYMENT'}
    if model_source == 'file':
        values['TCG_MODEL_NAME'] = 'file-deployment'
    if model_source == 'process':
        values['TCG_MODEL_NAME'] = 'file-deployment'
        monkeypatch.setenv('TCG_MODEL_NAME', 'process-deployment')
    write_env(tmp_path, values)
    assert Settings(tmp_path).value['model'] == f'{model_source}-deployment'


@pytest.mark.parametrize('missing', list(AZURE_ENV))
def test_partial_azure_configuration_fails_without_legacy_fallback(tmp_path, missing):
    values = {key: value for key, value in AZURE_ENV.items() if key != missing}
    write_env(tmp_path, values)
    with pytest.raises(DomainError) as error:
        Settings(tmp_path)
    assert missing in str(error.value)
    assert 'azure-test-key' not in str(error.value)


def test_process_empty_azure_field_clears_file_value_and_cannot_fall_back(tmp_path, monkeypatch):
    write_env(tmp_path, AZURE_ENV)
    monkeypatch.setenv('AZURE_OPENAI_API_KEY', '')
    with pytest.raises(DomainError, match='AZURE_OPENAI_API_KEY'):
        Settings(tmp_path)


def test_all_empty_azure_variables_leave_legacy_connection_active(tmp_path):
    save_legacy(tmp_path)
    write_env(tmp_path, {name: '' for name in AZURE_ENV})
    settings = Settings(tmp_path)
    assert settings.value['provider'] == 'openai'
    assert settings.environment_managed is False
    assert settings.secret() == 'legacy-test-key'


@pytest.mark.parametrize(('field', 'bad_value'), [
    ('AZURE_OPENAI_ENDPOINT', 'https://resource.openai.azure.com/openai/deployments/test'),
    ('AZURE_OPENAI_ENDPOINT', 'https://resource.openai.azure.com/?api-key=PRIVATE-KEY'),
    ('AZURE_OPENAI_ENDPOINT', 'https://user:PRIVATE-KEY@resource.openai.azure.com'),
    ('AZURE_API_VERSION', '2025-01-01-preview&key=PRIVATE-KEY'),
    ('AZURE_API_VERSION', '2025-02-30'),
    ('AZURE_OPENAI_DEPLOYMENT', '../PRIVATE-KEY'),
    ('AZURE_OPENAI_DEPLOYMENT', 'test?key=PRIVATE-KEY'),
])
def test_invalid_azure_routing_values_fail_safely(tmp_path, field, bad_value):
    write_env(tmp_path, {**AZURE_ENV, field: bad_value})
    with pytest.raises(DomainError) as error:
        Settings(tmp_path)
    assert 'PRIVATE-KEY' not in str(error.value)


def test_azure_environment_cannot_overwrite_saved_settings(tmp_path):
    original = save_legacy(tmp_path)
    write_env(tmp_path, AZURE_ENV)
    settings = Settings(tmp_path)
    with pytest.raises(DomainError, match='环境变量管理'):
        settings.save({'provider': 'openai', 'base_url': 'https://other.example.test', 'model': 'other'})
    assert (tmp_path / 'settings.json').read_bytes() == original
