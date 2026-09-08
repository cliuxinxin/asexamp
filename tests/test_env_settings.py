import json

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.model import Settings
from tcg.schemas import DomainError


def env_text(endpoint='https://api.example.test/v1', key='TEST-${LITERAL}-KEY'):
    return (f'TCG_MODEL_PROVIDER=openai\nTCG_MODEL_BASE_URL={endpoint}\n'
            'TCG_MODEL_NAME=test-model\nTCG_MODEL_TIMEOUT_SECONDS=300\n'
            f'TCG_API_KEY="{key}"\n')


def test_data_env_survives_new_settings_instances_without_exposing_key(tmp_path):
    (tmp_path / '.env').write_text(env_text())
    for _ in range(2):
        settings = Settings(tmp_path)
        assert settings.configured()
        assert settings.public()['provider'] == 'openai'
        assert settings.public()['timeout_seconds'] == 3600
        assert settings.secret() == 'TEST-${LITERAL}-KEY'
        assert settings.public()['has_api_key'] is True
        assert settings.public()['environment_managed'] is True
        assert 'TEST-' not in json.dumps(settings.public())
    assert not (tmp_path / 'settings.json').exists()


def test_process_environment_wins_and_explicit_file_wins_over_data_env(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text(env_text(key='DATA-KEY'))
    chosen = tmp_path / 'chosen.env'
    chosen.write_text(env_text(key='CHOSEN-KEY'))
    monkeypatch.setenv('TCG_ENV_FILE', str(chosen))
    monkeypatch.setenv('TCG_MODEL_NAME', 'process-model')
    monkeypatch.setenv('TCG_API_KEY', 'PROCESS-KEY')
    settings = Settings(tmp_path)
    assert settings.public()['model'] == 'process-model'
    assert settings.secret() == 'PROCESS-KEY'
    assert settings.public()['env_file'] == str(chosen)


def test_project_env_fallback_and_data_file_selection_are_independent_of_cwd(tmp_path, monkeypatch):
    from tcg import environment
    project = tmp_path / 'project'
    project.mkdir()
    data = tmp_path / 'data'
    data.mkdir()
    (project / '.env').write_text(env_text(key='PROJECT-KEY'))
    monkeypatch.setattr(environment, 'PROJECT_ROOT', project)
    monkeypatch.chdir(tmp_path)
    assert Settings(data).secret() == 'PROJECT-KEY'
    (data / '.env').write_text(env_text(key='DATA-KEY'))
    assert Settings(data).secret() == 'DATA-KEY'


def test_env_quotes_comments_export_and_command_text_are_literal(tmp_path):
    marker = tmp_path / 'must-not-exist'
    (tmp_path / '.env').write_text(
        'export TCG_MODEL_NAME="quoted model" # comment\n'
        f"TCG_API_KEY='literal# $(touch {marker}) ${{KEEP}}'\n")
    settings = Settings(tmp_path)
    assert settings.public()['model'] == 'quoted model'
    assert settings.secret() == f'literal# $(touch {marker}) ${{KEEP}}'
    assert not marker.exists()


def test_endpoint_override_drops_lower_priority_credential(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    settings.save({'provider':'openai', 'base_url':'https://old.example.test/v1', 'model':'saved', 'timeout_seconds':120, 'api_key':'SAVED-KEY'})
    (tmp_path / '.env').write_text(env_text(key='FILE-KEY'))
    monkeypatch.setenv('TCG_MODEL_BASE_URL', 'https://different.example.test/v1')
    settings = Settings(tmp_path)
    assert settings.public()['base_url'] == 'https://different.example.test/v1'
    assert settings.secret() == ''
    assert settings.public()['has_api_key'] is False


def test_partial_env_preserves_saved_key_for_same_endpoint_and_empty_key_clears(tmp_path):
    settings = Settings(tmp_path)
    settings.save({'provider':'openai', 'base_url':'https://old.example.test/v1', 'model':'saved', 'timeout_seconds':120, 'api_key':'SAVED-KEY'})
    (tmp_path / '.env').write_text('TCG_MODEL_TIMEOUT_SECONDS=300\n')
    assert Settings(tmp_path).secret() == 'SAVED-KEY'
    (tmp_path / '.env').write_text('TCG_API_KEY=\n')
    assert Settings(tmp_path).secret() == ''


@pytest.mark.parametrize('line', [
    'TCG_MODEL_TIMEOUT_SECONDS=PRIVATE-NOT-A-NUMBER',
    'TCG_MODEL_TIMEOUT_SECONDS=0',
    'TCG_MODEL_PROVIDER=PRIVATE-BAD-PROVIDER',
    'TCG_MODEL_BASE_URL=https://user:PRIVATE-PASSWORD@example.test/v1',
    'TCG_API_KEY="PRIVATE-UNTERMINATED',
])
def test_invalid_env_fails_with_safe_message(tmp_path, line):
    (tmp_path / '.env').write_text(line + '\n')
    with pytest.raises(DomainError) as error:
        Settings(tmp_path)
    assert 'PRIVATE-' not in str(error.value)


def test_missing_explicit_file_is_visible_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('TCG_ENV_FILE', str(tmp_path / 'missing.env'))
    with pytest.raises(DomainError, match='env'):
        Settings(tmp_path)


def test_env_managed_settings_api_is_read_only_and_does_not_leak_key(tmp_path):
    (tmp_path / '.env').write_text(env_text())
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/api/health').json()['model_configured'] is True
        settings = client.get('/api/settings')
        assert settings.json()['environment_managed'] is True
        assert 'TEST-' not in settings.text
        response = client.put('/api/settings', json={'provider':'openai','base_url':'https://another.example.test/v1','model':'other'})
        assert response.status_code == 409
        assert 'TEST-' not in response.text
    assert 'TEST-' not in (tmp_path / 'logs' / 'tcg.log').read_text()
