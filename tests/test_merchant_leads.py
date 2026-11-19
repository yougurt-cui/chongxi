import sqlite3

import pytest
from flask import Flask

from api.merchant_lead_api import merchant_lead_api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('MERCHANT_LEADS_DB', str(tmp_path / 'leads.sqlite3'))
    monkeypatch.setenv('MINIPROGRAM_ADMIN_TOKEN', 'test-admin-token')
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY='test-only')
    app.register_blueprint(merchant_lead_api)
    return app.test_client()


@pytest.fixture
def payload():
    return dict(company='测试店铺', contact='测试联系人', phone='13800000000',
                category='宠物用品', message='希望了解合作方式', consent=True)


def test_save_retrieve_and_deduplicate(client, payload):
    first = client.post('/api/merchant-leads', json=payload)
    assert first.status_code == 201
    assert client.post('/api/merchant-leads', json=payload).json['id'] == first.json['id']
    assert client.get('/api/admin/merchant-leads').status_code == 401
    response = client.get('/api/admin/merchant-leads', headers={'X-Admin-Token': 'test-admin-token'})
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert len(response.json['items']) == 1
    assert response.json['items'][0]['company'] == payload['company']
    assert response.json['items'][0]['consent_version'] == 'merchant-contact-v1'


@pytest.mark.parametrize('change', [dict(phone='123'), dict(consent=False),
    dict(company=' '), dict(category='无效品类'), dict(message='长' * 1001), dict(contact=['错误'])])
def test_reject_invalid(client, payload, change):
    payload.update(change)
    assert client.post('/api/merchant-leads', json=payload).status_code == 400


def test_bad_json_and_body_limit(client):
    assert client.post('/api/merchant-leads', json=[]).status_code == 400
    assert client.post('/api/merchant-leads', data='{', content_type='application/json').status_code == 400
    assert client.post('/api/merchant-leads', data='x' * 16385, content_type='application/json').status_code == 413
    assert client.post('/api/merchant-leads', data='company=a').status_code == 415


def test_rate_limit_and_retry(client, payload):
    for i in range(10):
        payload['company'] = '测试店铺%s' % i
        assert client.post('/api/merchant-leads', json=payload).status_code == 201
    # A retry of an already saved request is safe even after reaching the cap.
    assert client.post('/api/merchant-leads', json=payload).status_code == 201
    payload['company'] = '新店铺'
    assert client.post('/api/merchant-leads', json=payload).status_code == 429


def test_storage_failure_never_reports_success(client, payload, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError('private database path')
    monkeypatch.setattr('api.merchant_lead_api.save_lead', fail)
    response = client.post('/api/merchant-leads', json=payload)
    assert response.status_code == 503
    assert response.json['ok'] is False
    assert 'private database path' not in response.get_data(as_text=True)
