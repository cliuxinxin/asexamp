import io
import zipfile

from fastapi.testclient import TestClient
from tcg.main import create_app
from tcg.documents import MAX_UPLOAD
from test_backend_api import Model, setup_chat


def test_upload_unknown_role_and_correct_it(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        r = client.post('/api/chats/' + chat['id'] + '/sources', data={'role': 'auto'},
                        files={'file': ('变更说明.txt', '退款期限改为7天。'.encode())})
        assert r.status_code == 200, r.text
        source = r.json()
        assert source['role'] == 'change'
        assert source['classification']['provisional'] is True
        r = client.put('/api/sources/' + source['id'] + '/role', json={'role': 'primary'})
        assert r.status_code == 200 and r.json()['classification']['provisional'] is False


def test_upload_limit_allows_more_than_old_15mb():
    assert MAX_UPLOAD >= 100 * 1024 * 1024


def test_duplicate_upload_reuses_source(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        path = '/api/chats/' + chat['id'] + '/sources'
        a = client.post(path, data={'role': 'primary'}, files={'file': ('same.txt', b'The user must sign in.')})
        b = client.post(path, data={'role': 'primary'}, files={'file': ('same.txt', b'The user must sign in.')})
        assert a.status_code == b.status_code == 200
        assert a.json()['id'] == b.json()['id']
