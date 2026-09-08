"""Exercise the distributed launcher and compiled UI over a real loopback socket."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest


@pytest.mark.skipif(os.name == 'nt', reason='Process-group cleanup is POSIX-specific')
@pytest.mark.parametrize('env_configured', [False, True])
def test_launcher_serves_prebuilt_ui_and_local_api_with_optional_env_file(tmp_path, env_configured):
    root = Path(__file__).resolve().parents[1]
    arguments = []
    if env_configured:
        config = tmp_path / 'chosen.env'
        config.write_text('TCG_MODEL_PROVIDER=openai\nTCG_MODEL_BASE_URL=http://127.0.0.1:1234/v1\nTCG_MODEL_NAME=test-env\nTCG_API_KEY=LAUNCHER-TEST-KEY\nTCG_MODEL_TIMEOUT_SECONDS=300\n')
        arguments = ['--env-file', str(config)]
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, str(root / 'start.py'), '--no-install', '--no-browser',
         '--port', str(port), '--data-dir', str(tmp_path), *arguments],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
            deadline = time.monotonic() + 20
            while True:
                try:
                    health = client.get('/api/health')
                    if health.status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                if process.poll() is not None:
                    pytest.fail(process.communicate()[0])
                assert time.monotonic() < deadline, 'Launcher did not become healthy'
                time.sleep(.1)
            assert health.json()['storage'] == 'local'
            assert health.json()['model_configured'] is env_configured
            if env_configured:
                settings = client.get('/api/settings')
                assert settings.json()['model'] == 'test-env'
                assert settings.json()['timeout_seconds'] == 3600
                assert settings.json()['has_api_key'] is True
                assert 'LAUNCHER-TEST-KEY' not in settings.text
            html = client.get('/')
            assert html.status_code == 200
            assert 'TCG Case Agent' in html.text
            import re
            scripts = re.findall(r'src="([^"]+\.js)"', html.text)
            assert scripts
            assert client.get(scripts[0]).status_code == 200
            project = client.get('/api/projects').json()[0]
            chat = client.post('/api/projects/' + project['id'] + '/chats', json={}).json()
            if not env_configured:
                response = client.post('/api/chats/' + chat['id'] + '/messages', json={'content': '生成用例'})
                assert response.status_code == 400
            assert client.get('/api/chats/' + chat['id']).json()['messages'] == []
            assert client.post('/api/projects', json={'name': 'invalid'}, headers={'Origin': 'https://unrelated.example'}).status_code == 403
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
