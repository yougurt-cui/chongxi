from io import BytesIO

import pytest
import requests
from PIL import Image

from services import chengwu_pet_badge as badge


def response(status, body, retry=None):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r._content_consumed = True
    if retry:
        r.headers['Retry-After'] = retry
    return r


def test_submit_and_download(monkeypatch):
    image = BytesIO()
    Image.new('RGB', (1024, 1024)).save(image, 'PNG')
    replies = iter([
        response(202, b'{"id":"job_1"}'),
        response(200, b'{"status":"processing"}', '9'),
        response(200, b'{"status":"succeeded"}'),
        response(200, image.getvalue()),
    ])
    calls = []
    def send(method, url, **options):
        calls.append((method, url, options))
        return next(replies)
    monkeypatch.setattr(requests, 'request', send)
    monkeypatch.setenv('CHENGWU_API_KEY', 'test')
    assert badge.submit(b'photo', 'felt-relief', 'operation') == ('job_1', 3)
    with pytest.raises(badge.RetryLater) as pending:
        badge.poll('job_1')
    assert pending.value.delay == 9
    assert badge.poll('job_1').read() == image.getvalue()
    assert calls[0][2]['headers']['Idempotency-Key'] == 'operation'
    assert calls[0][2]['data'] == {'style': 'felt-relief'}
    assert calls[0][2]['files']['image'][1] == b'photo'
    assert calls[-1][1] == badge.BASE + '/job_1/image'
    assert all(c[2]['headers']['Authorization'] == 'Bearer test' for c in calls)


@pytest.mark.parametrize('status,body,error', [
    (429, b'{}', badge.RetryLater),
    (503, b'{}', badge.RetryLater),
    (409, b'{"reason":"image_not_ready"}', badge.RetryLater),
    (200, b'{"id":"../../elsewhere"}', badge.RetryLater),
    (403, b'{"reason":"model_access_denied"}', badge.BadgeFailed),
])
def test_vendor_failures(monkeypatch, status, body, error):
    monkeypatch.setattr(requests, 'request', lambda *a, **kw: response(status, body))
    with pytest.raises(error):
        badge.submit(b'image', 'washi-collage', 'operation')


def test_uncertain_charge_includes_receipt(monkeypatch):
    task = b'{"status":"failed","reason":"generation_status_unknown","upstream_request_id":"ref17"}'
    monkeypatch.setattr(requests, 'request', lambda *a, **kw: response(200, task))
    with pytest.raises(badge.BadgeFailed, match='ref17'):
        badge.poll('job_1')


def test_invalid_png(monkeypatch):
    replies = iter([response(200, b'{"status":"succeeded"}'), response(200, b'not png')])
    monkeypatch.setattr(requests, 'request', lambda *a, **kw: next(replies))
    with pytest.raises(badge.BadgeFailed, match='核对'):
        badge.poll('job_1')
