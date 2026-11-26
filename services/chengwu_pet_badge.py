"""Server-side client for Chengwu's asynchronous pet badge template."""
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO

import requests
from PIL import Image

BASE = 'https://www.chengwuos.shop/v1/templates/pet-badge'
MODEL = 'chengwu:gpt-image-2.6-flare'
STYLES = {
    'washi-collage': '纸胶带拼贴', 'korean-flat': '韩式稚拙扁平插画',
    'japanese-ink': '日系自由线描', 'lifestyle-doodle': '生活涂鸦插画',
    'naive-hand-drawn': '稚拙手绘', 'felt-relief': '毛毡浅浮雕',
}
MAX_BYTES = 10 * 1024 * 1024
REVIEW = '请联系管理员核对该笔生成记录后再尝试'


def api_key():
    return os.getenv('CHENGWU_API_KEY', '').strip()


class RetryLater(Exception):
    def __init__(self, delay=3):
        super().__init__('服务暂忙，请稍后重试')
        self.delay = delay


class BadgeFailed(Exception):
    pass


def retry_delay(value):
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds() + 1
        except (TypeError, ValueError, OverflowError):
            seconds = 3
    return min(86400, max(3, int(seconds)))


def failure(reason, receipt=None):
    if reason in ('generation_status_unknown', 'invalid_image_result'):
        ref = str(receipt or '')
        return BadgeFailed(REVIEW + ('（回执 ' + ref + '）' if re.fullmatch(r'[\w.-]{1,100}', ref) else ''))
    return BadgeFailed({
        'image_rejected': '照片未通过审核，请更换照片',
        'insufficient_credits': '徽章额度不足，请联系管理员',
        'job_not_found': '徽章任务已过期或不可用',
    }.get(reason, '徽章转绘失败，请稍后重试'))


def request(method, suffix='', **kwargs):
    headers = {'Authorization': 'Bearer ' + api_key(), **kwargs.pop('headers', {})}
    response = requests.request(method, BASE + suffix, headers=headers, timeout=(5, 20),
                                allow_redirects=False, **kwargs)
    if 200 <= response.status_code < 300:
        return response
    with response:
        if response.status_code in (408, 429) or response.status_code >= 500:
            raise RetryLater(retry_delay(response.headers.get('Retry-After')))
        try:
            body = response.json()
        except ValueError:
            body = {}
        if body.get('reason') == 'image_not_ready':
            raise RetryLater()
        raise failure(body.get('reason'), body.get('upstream_request_id'))


def submit(content, style, operation_id):
    with request('POST', files={'image': ('pet.jpg', content, 'image/jpeg')},
                 data={'style': style}, headers={'Idempotency-Key': operation_id}) as response:
        receipt = response.json().get('id')
        if not isinstance(receipt, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', receipt):
            raise RetryLater()
        return receipt, retry_delay(response.headers.get('Retry-After'))


def poll(receipt):
    with request('GET', '/' + receipt) as response:
        task = response.json()
        if task.get('status') == 'failed':
            raise failure(task.get('reason'), task.get('upstream_request_id'))
        if task.get('status') != 'succeeded':
            raise RetryLater(retry_delay(response.headers.get('Retry-After')))
    with request('GET', '/' + receipt + '/image', stream=True) as response:
        output = BytesIO()
        for chunk in response.iter_content(65536):
            output.write(chunk)
            if output.tell() > MAX_BYTES:
                raise failure('invalid_image_result')
    output.seek(0)
    try:
        with Image.open(output) as image:
            if image.format != 'PNG' or image.size != (1024, 1024):
                raise failure('invalid_image_result')
            image.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise failure('invalid_image_result') from exc
    output.seek(0)
    return output
