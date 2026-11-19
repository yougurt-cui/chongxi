import base64
from io import BytesIO
from types import SimpleNamespace
import threading

import pytest
from flask import Flask
from PIL import Image
from werkzeug.datastructures import FileStorage

from services import miniprogram_image_generation_service as service
from api import miniprogram_image_generation_api as api


def photo(fmt='PNG', size=(80, 60)):
    out = BytesIO()
    Image.new('RGB', size, 'orange').save(out, fmt)
    return out.getvalue()


def upload(content=None):
    return FileStorage(stream=BytesIO(photo() if content is None else content), filename='pet.png')


@pytest.mark.parametrize('prompt', ['', ' ', '猫' * 301])
def test_prompt_rejected(prompt):
    with pytest.raises(ValueError):
        service.validate_input(upload(), prompt, 'r' * 16)


def test_normalize_and_unicode():
    content, prompt, _ = service.validate_input(upload(photo(size=(3000, 1500))), '🐈' * 300, 'r' * 16)
    with Image.open(BytesIO(content)) as image:
        assert image.format == 'JPEG'
        assert image.size == (2048, 1024)
    assert len(prompt) == 300


@pytest.mark.parametrize('content', [b'bad', photo('GIF'), photo(size=(14, 20)), photo(size=(500, 20)), b'x' * (service.MAX_BYTES + 1)])
def test_bad_image(content):
    with pytest.raises(ValueError):
        service.validate_input(upload(content), '猫', 'r' * 16)


@pytest.fixture
def runtime(monkeypatch):
    rows, jobs, calls = {}, [], []
    monkeypatch.setattr(service, 'init_tables', lambda: None)
    monkeypatch.setattr(service, '_expire', lambda user: None)
    monkeypatch.setattr(service, 'get_image_config', lambda: dict(api_key='test-key', model='test-model', base_url='https://ark.test/api/v3'))
    monkeypatch.setattr(service, '_slots', threading.BoundedSemaphore(4))
    def find(user, *, task_id=None, request_id=None):
        return next((r.copy() for r in rows.values() if r['user_id'] == user and (r['id'] == task_id if task_id else r['request_id'] == request_id)), None)
    def insert(row):
        if any(r['user_id'] == row['user_id'] and r['status'] in ('pending', 'generating') for r in rows.values()):
            raise service.pymysql.IntegrityError(1062, 'duplicate')
        rows[row['id']] = row.copy()
    def update(task_id, status, **kw):
        if rows[task_id]['status'] in ('pending', 'generating'):
            rows[task_id].update(status=status, **kw)
    monkeypatch.setattr(service, '_find', find)
    monkeypatch.setattr(service, '_insert', insert)
    monkeypatch.setattr(service, '_update', update)
    monkeypatch.setattr(service, '_executor', SimpleNamespace(submit=lambda fn, *args: jobs.append((fn, args))))
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def json(self): return {'data': [{'b64_json': base64.b64encode(photo()).decode()}]}
    def post(url, **kw):
        calls.append((url, kw))
        return Response()
    monkeypatch.setattr(service.requests, 'post', post)
    monkeypatch.setattr(service, 'upload_moment_image', lambda image, **kw: {'ok': True, 'item': {'url': '/api/miniprogram/moment-images/output'}})
    def auth(token):
        if token not in ('alice', 'bob'): raise PermissionError()
        return {'id': token}
    monkeypatch.setattr(api, 'require_user_by_token', auth)
    app = Flask(__name__)
    app.register_blueprint(api.image_generation_api)
    return app.test_client(), rows, jobs, calls


URL = '/api/miniprogram/image-generations'

def submit(client, prompt='画猫', request_id='r' * 16):
    return client.post(URL, headers={'Authorization': 'Bearer alice'}, data={'image': (BytesIO(photo()), 'cat.png'), 'prompt': prompt, 'request_id': request_id})


def test_full_flow_idempotency_and_owner(runtime):
    client, rows, jobs, calls = runtime
    assert client.post(URL).status_code == 401
    created = submit(client)
    assert created.status_code == 202
    task = created.json['item']
    assert task['status'] == 'pending'
    assert submit(client).json['item']['id'] == task['id']
    assert len(jobs) == 1
    assert submit(client, prompt='画狗').status_code == 400
    assert submit(client, request_id='s' * 16).status_code == 429
    assert client.get(URL + '/' + task['id'], headers={'Authorization': 'Bearer bob'}).status_code == 404
    fn, args = jobs.pop()
    fn(*args)
    result = client.get(URL + '/' + task['id'], headers={'Authorization': 'Bearer alice'})
    assert result.json['item']['status'] == 'succeeded'
    assert result.json['item']['image_url'].endswith('/output')
    assert set(result.json['item']) == {'id', 'status', 'image_url', 'error'}
    url, options = calls[0]
    assert url.endswith('/images/generations')
    assert options['json']['image'].startswith('data:image/jpeg;base64,')
    assert options['json']['prompt'] == '画猫'
    assert options['json']['size'] == '2048x2048'
    assert options['json']['model'] == 'test-model'
    assert submit(client, request_id='s' * 16).status_code == 202


def test_worker_failure_releases_slot(runtime, monkeypatch):
    client, rows, jobs, calls = runtime
    def fail(*args): raise RuntimeError('secret provider error')
    monkeypatch.setattr(service, 'generate_reference_image', fail)
    task = submit(client).json['item']
    fn, args = jobs.pop()
    fn(*args)
    assert rows[task['id']]['status'] == 'failed'
    assert 'secret' not in rows[task['id']]['error']
    for _ in range(4): assert service._slots.acquire(blocking=False)
    assert not service._slots.acquire(blocking=False)


def test_missing_configuration(runtime, monkeypatch):
    monkeypatch.setattr(service, 'get_image_config', lambda: {'api_key': ''})
    assert submit(runtime[0]).status_code == 503


def test_seedream_55_request_is_single_reference_generation(runtime, monkeypatch):
    client,rows,jobs,calls=runtime
    monkeypatch.setattr(service,'get_image_config',lambda:dict(api_key='test-key',model='doubao-seedream-5-5-261028',base_url='https://ark.test/api/v3'))
    submit(client)
    fn,args=jobs.pop();fn(*args)
    payload=calls[0][1]['json']
    assert payload['model']=='doubao-seedream-5-5-261028'
    assert payload['output_format']=='jpeg' and payload['response_format']=='b64_json'
    assert 'sequential_image_generation' not in payload and 'stream' not in payload
    assert not payload.get('layer_decomposition')


def test_reference_model_default_is_isolated_from_shared_ark_config(monkeypatch):
    monkeypatch.delenv('MINIPROGRAM_IMAGE_MODEL',raising=False)
    monkeypatch.delenv('MINIPROGRAM_IMAGE_API_KEY',raising=False)
    shared=dict(model='doubao-seedream-4-0-250828',api_key='test',base_url='https://ark.test/api/v3')
    monkeypatch.setattr(service,'get_ark_image_config',lambda:dict(shared))
    assert service.get_image_config()['model']=='doubao-seedream-5-5-261028'
    assert shared['model']=='doubao-seedream-4-0-250828'
    monkeypatch.setenv('MINIPROGRAM_IMAGE_API_KEY','new-test-key')
    assert service.get_image_config()['api_key']=='new-test-key'
    assert shared['api_key']=='test'
    monkeypatch.setenv('MINIPROGRAM_IMAGE_MODEL','doubao-seedream-4-0-250828')
    assert service.get_image_config()['model']=='doubao-seedream-4-0-250828'
