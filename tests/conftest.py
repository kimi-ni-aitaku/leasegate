import base64
import os
import secrets
from dataclasses import dataclass
import fakeredis
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.models import Base


@dataclass
class Clock:
    value: int = 1800000000
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def system(tmp_path):
    database_url = os.getenv('TEST_DATABASE_URL', 'sqlite:///' + str(tmp_path / 'test.db'))
    if not database_url.startswith('sqlite') and not database_url.split('?')[0].endswith('_test'):
        raise RuntimeError('TEST_DATABASE_URL must use a dedicated database ending in _test')
    settings = Settings(_env_file=None, database_url=database_url, signing_key=secrets.token_urlsafe(48),
                        encryption_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                        webhook_secret=secrets.token_urlsafe(48), admin_token=secrets.token_urlsafe(48))
    database = Database(settings.database_url)
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    clock, cache = Clock(), fakeredis.FakeRedis()
    app = create_app(settings, database, cache, clock)
    with TestClient(app) as client:
        csrf = client.get('/api/csrf').json()['token']
        client.headers['X-CSRF-Token'] = csrf
        yield app.state.service, client, app
    database.engine.dispose()


def create_order(client):
    response = client.post('/api/demo/orders', json={'product_id': 'test-60s'})
    assert response.status_code == 200, response.text
    return response.json()


def prepare_qr(client):
    order = create_order(client)
    assert client.post('/api/activation/redeem', json={'code': order['code']}).status_code == 200
    qr = client.post('/api/qr/create', json={})
    assert qr.status_code == 200, qr.text
    return order, qr.json()


def activate(client):
    order, qr = prepare_qr(client)
    assert client.post(f'/api/qr/{qr["id"]}/scanned', json={'raw': qr['url']}).status_code == 200
    confirmed = client.post(f'/api/qr/{qr["id"]}/confirm', json={'raw': qr['url']})
    assert confirmed.status_code == 200, confirmed.text
    result = client.post('/api/activation/result', json={})
    assert result.status_code == 200, result.text
    return order, qr, result.json()
