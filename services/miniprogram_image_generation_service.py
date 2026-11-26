"""Authenticated reference-image generation with durable task status and idempotency."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import threading
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pymysql
import requests
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage

from app_config import get_ark_image_config, get_mysql_config
from services.miniprogram_moment_image_service import upload_moment_image
from services.miniprogram_image_prompts import PROMPT_LIMIT
from services import chengwu_pet_badge as badge

def get_image_config():
    config = get_ark_image_config()
    config['api_key'] = os.getenv('MINIPROGRAM_IMAGE_API_KEY', '').strip() or config['api_key']
    config['model'] = os.getenv('MINIPROGRAM_IMAGE_MODEL', '').strip() or 'doubao-seedream-5-5-261028'
    return config


TABLE = 'miniprogram_image_generation'
MAX_BYTES = 10 * 1024 * 1024

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='pet-image')
_slots = threading.BoundedSemaphore(4)
_schema_lock = threading.Lock()
_schema_ready = False
logger = logging.getLogger(__name__)


class BusyError(Exception):
    pass


class ConfigurationError(Exception):
    pass


def _connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor)


def init_tables():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
                    id CHAR(32) PRIMARY KEY,
                    user_id VARCHAR(128) NOT NULL,
                    request_id VARCHAR(64) NOT NULL,
                    input_hash CHAR(64) NOT NULL,
                    prompt TEXT NOT NULL,
                    model VARCHAR(128) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    active_user VARCHAR(128) NULL,
                    image_url VARCHAR(2048) NULL,
                    error VARCHAR(300) NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY user_request (user_id, request_id),
                    UNIQUE KEY one_active_task (active_user),
                    KEY user_created (user_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
                cursor.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}_badge (
                    task_id CHAR(32) PRIMARY KEY, receipt VARCHAR(128) NULL, next_check DATETIME NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            conn.commit()
        _schema_ready = True


def _expire(user_id):
    # A crashed worker cannot leave an account permanently locked.
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE {TABLE} SET status='failed',active_user=NULL,error=%s,updated_at=UTC_TIMESTAMP() WHERE user_id=%s AND status IN ('pending','generating') AND model<>%s AND created_at < UTC_TIMESTAMP() - INTERVAL 10 MINUTE",
                           ('生成超时，请重新尝试', user_id, badge.MODEL))
            # Closed pages may resume a paid badge during the provider's 24-hour window.
            cursor.execute(f"""UPDATE {TABLE} SET active_user=NULL
                WHERE user_id=%s AND model=%s AND active_user IS NOT NULL
                AND created_at < UTC_TIMESTAMP() - INTERVAL 10 MINUTE""", (user_id, badge.MODEL))
            cursor.execute(f"""UPDATE {TABLE} SET status='failed',active_user=NULL,error='徽章任务已过期'
                WHERE user_id=%s AND model=%s AND status IN ('pending','generating')
                AND created_at < UTC_TIMESTAMP() - INTERVAL 24 HOUR""", (user_id, badge.MODEL))
        conn.commit()


def _find(user_id, *, task_id=None, request_id=None):
    column, value = ('id', task_id) if task_id else ('request_id', request_id)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'SELECT * FROM {TABLE} WHERE user_id=%s AND {column}=%s LIMIT 1', (user_id, value))
            return cursor.fetchone()


def _insert(row):
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"INSERT INTO {TABLE} (id,user_id,request_id,input_hash,prompt,model,status,active_user,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,UTC_TIMESTAMP(),UTC_TIMESTAMP())",
                           (row['id'], row['user_id'], row['request_id'], row['input_hash'], row['prompt'], row['model'], row['user_id']))
            if row['model'] == badge.MODEL:
                cursor.execute(f'INSERT INTO {TABLE}_badge (task_id) VALUES (%s)', (row['id'],))
        conn.commit()


def _update(task_id, status, *, image_url=None, error=None):
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE {TABLE} SET status=%s,image_url=%s,error=%s,active_user=IF(%s IN ('succeeded','failed'),NULL,active_user),updated_at=UTC_TIMESTAMP() WHERE id=%s AND status IN ('pending','generating')",
                           (status, image_url, error, status, task_id))
        conn.commit()


def _public(row):
    return {key: row.get(key) for key in ('id', 'status', 'image_url', 'error')}


def validate_input(image_file, prompt, request_id):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_LIMIT:
        raise ValueError(f'描述不能为空，且不能超过 {PROMPT_LIMIT} 字')
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,64}', request_id):
        raise ValueError('request_id 格式不正确')
    if image_file is None:
        raise ValueError('请上传参考照片')
    content = image_file.stream.read(MAX_BYTES + 1)
    if not content or len(content) > MAX_BYTES:
        raise ValueError('图片不能为空，且不能超过 10MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                if image.format not in ('JPEG', 'PNG'):
                    raise ValueError('仅支持 JPG / PNG 图片')
                if min(width, height) <= 14 or width * height > 36000000 or not 1 / 16 <= width / height <= 16:
                    raise ValueError('照片尺寸不支持')
                image.load()
                normalized = ImageOps.exif_transpose(image)
                normalized.thumbnail((2048, 2048))
                background = Image.new('RGB', normalized.size, 'white')
                rgba = normalized.convert('RGBA')
                background.paste(rgba, mask=rgba.getchannel('A'))
                output = BytesIO()
                background.save(output, format='JPEG', quality=94)
                return output.getvalue(), prompt.strip(), request_id
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('图片无法读取，请重新选择') from exc


def _advance_badge(row, content=None):
    submitted = False
    if row['status'] not in ('pending', 'generating'):
        return _public(row), submitted
    if not badge.api_key():
        raise ConfigurationError('徽章转绘尚未配置')
    task_id = row['id']
    # Serialize calls per task. Persist the receipt and retry clock across restarts.
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"""SELECT receipt, next_check > UTC_TIMESTAMP() AS waiting
                FROM {TABLE}_badge WHERE task_id=%s FOR UPDATE""", (task_id,))
            job = cursor.fetchone()
            cursor.execute(f'SELECT * FROM {TABLE} WHERE id=%s', (task_id,))
            row = cursor.fetchone()
            if row['status'] not in ('pending', 'generating'):
                return _public(row), submitted
            receipt = job['receipt']
            if job['waiting'] or (not receipt and content is None):
                if not receipt and content is not None:
                    raise BusyError('徽章服务暂忙，请稍后重试')
                return _public(row), submitted
            delay = 3
            try:
                if not receipt:
                    receipt, delay = badge.submit(content, row['prompt'], task_id)
                    submitted = True
                    row['status'] = 'generating'
                else:
                    png = badge.poll(receipt)
                    image = FileStorage(stream=png, filename=f'guzi-{task_id}.png', content_type='image/png')
                    saved = upload_moment_image(image, user_id=row['user_id'])
                    row.update(status='succeeded', image_url=saved['item']['url'])
            except badge.BadgeFailed as exc:
                row.update(status='failed', error=str(exc))
            except badge.RetryLater as exc:
                delay = exc.delay
            except (requests.RequestException, ValueError):
                # Reuse the operation ID after an ambiguous submission response.
                delay = 5
            cursor.execute(f"""UPDATE {TABLE}_badge SET receipt=%s,
                next_check=DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s SECOND) WHERE task_id=%s""",
                           (receipt, delay, task_id))
            cursor.execute(f"""UPDATE {TABLE} SET status=%s,image_url=%s,error=%s,
                active_user=IF(%s IN ('succeeded','failed'),NULL,active_user),
                updated_at=UTC_TIMESTAMP() WHERE id=%s""",
                           (row['status'], row.get('image_url'), row.get('error'), row['status'], task_id))
        conn.commit()
    if not receipt and row['status'] != 'failed':
        raise BusyError('徽章服务暂忙，请稍后重试')
    return _public(row), submitted


def _finish_badge(user_id, task_id):
    # Continue collecting the image if the user closes the page. A later GET
    # can resume from the saved receipt if this worker is interrupted.
    try:
        deadline = time.monotonic() + 570
        while time.monotonic() < deadline:
            time.sleep(3)
            row = _find(user_id, task_id=task_id)
            if not row:
                break
            task, _ = _advance_badge(row)
            if task['status'] in ('succeeded', 'failed'):
                break
    except Exception:
        logger.warning('Badge collection interrupted: task=%s', task_id)
    finally:
        _slots.release()


def create_task(user_id, image_file, prompt, request_id, style=None):
    if style is not None:
        if style not in badge.STYLES or prompt:
            raise ValueError('徽章转绘请选择一种预设风格')
        prompt = style
    content, prompt, request_id = validate_input(image_file, prompt, request_id)
    config = {'api_key': badge.api_key(), 'model': badge.MODEL} if style else get_image_config()
    if not config['api_key']:
        raise ConfigurationError('生图服务尚未配置')
    init_tables()
    _expire(user_id)
    digest = hashlib.sha256(content + b'\0' + prompt.encode('utf-8')).hexdigest()
    if style:
        digest = hashlib.sha256(('badge:' + digest).encode()).hexdigest()
    row = _find(user_id, request_id=request_id)
    if row:
        if row['input_hash'] != digest:
            raise ValueError('同一 request_id 不能用于不同照片、风格或描述')
        if not style or row['status'] != 'pending':
            return _public(row)
    if not _slots.acquire(blocking=False):
        raise BusyError('生成任务较多，请稍后再试')
    try:
        if row is None:
            row = dict(id=uuid.uuid4().hex, user_id=user_id, request_id=request_id,
                       input_hash=digest, prompt=prompt, model=config['model'], status='pending')
            try:
                _insert(row)
            except pymysql.IntegrityError:
                row = _find(user_id, request_id=request_id)
                if row is None:
                    raise BusyError('已有头像正在生成，请稍后重试')
                if row['input_hash'] != digest:
                    raise ValueError('同一 request_id 不能用于不同照片、风格或描述')
                if not style:
                    _slots.release()
                    return _public(row)
        if style:
            result, submitted = _advance_badge(row, content)
            if submitted:
                _executor.submit(_finish_badge, user_id, row['id'])
            else:
                _slots.release()
            return result
        try:
            _executor.submit(_run, row, content, config)
        except Exception:
            _update(row['id'], 'failed', error='任务启动失败，请重试')
            raise
    except Exception:
        _slots.release()
        raise
    return _public(row)


def get_task(user_id, task_id):
    if not re.fullmatch(r'[a-f0-9]{32}', task_id or ''):
        raise LookupError('任务不存在')
    init_tables()
    _expire(user_id)
    row = _find(user_id, task_id=task_id)
    if not row:
        raise LookupError('任务不存在')
    if row['model'] == badge.MODEL:
        task, _ = _advance_badge(row)
        return task
    return _public(row)


def generate_reference_image(content, prompt, config):
    # Base64 input avoids public reference URLs; base64 output avoids expiring vendor URLs.
    payload = {
        'model': config['model'], 'prompt': prompt,
        'image': 'data:image/jpeg;base64,' + base64.b64encode(content).decode('ascii'),
        'size': '2048x2048', 'response_format': 'b64_json', 'watermark': False,
    }
    if config['model'].startswith(('doubao-seedream-5-5', 'doubao-seedream-5-0-pro', 'doubao-seedream-5-0-flash')):
        payload['output_format'] = 'jpeg'
    else:
        payload.update(sequential_image_generation='disabled', stream=False)
    with requests.post(f"{config['base_url']}/images/generations", headers={
        'Authorization': f"Bearer {config['api_key']}", 'Content-Type': 'application/json',
    }, json=payload, timeout=(10, 180)) as response:
        response.raise_for_status()
        payload = response.json()
    items = payload.get('data') or []
    encoded = items[0].get('b64_json') if items and isinstance(items[0], dict) else None
    if not isinstance(encoded, str) or len(encoded) > 40 * 1024 * 1024:
        raise RuntimeError('生图结果为空或格式异常')
    binary = base64.b64decode(encoded, validate=True)
    with Image.open(BytesIO(binary)) as image:
        if image.width * image.height > 16777216:
            raise RuntimeError('生成图片尺寸异常')
        output = BytesIO()
        image.convert('RGB').save(output, 'JPEG', quality=94)
    if output.tell() > MAX_BYTES:
        raise RuntimeError('生成图片过大')
    return output.getvalue()


def _run(row, content, config):
    try:
        _update(row['id'], 'generating')
        generated = generate_reference_image(content, row['prompt'], config)
        saved = upload_moment_image(FileStorage(stream=BytesIO(generated), filename=f"guzi-{row['id']}.jpg", content_type='image/jpeg'), user_id=row['user_id'])
        _update(row['id'], 'succeeded', image_url=saved['item']['url'])
    except Exception:
        # Do not expose vendor response bodies, API keys or base64 photos to the client/logs.
        logger.warning('Reference image generation failed: task=%s', row['id'])
        try:
            _update(row['id'], 'failed', error='暂时无法生成，请稍后重试')
        except Exception:
            logger.warning('Unable to persist generation failure: task=%s', row['id'])
    finally:
        _slots.release()
