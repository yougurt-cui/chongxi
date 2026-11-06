"""Merchant certifications entered and maintained by workbench administrators."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import pymysql
from app_config import get_mysql_config

JUMP_TYPES = {'web': '网页链接', 'mini_program': '微信小程序', 'other': '其他方式'}
FIELDS = ('wechat_id', 'phone', 'merchant_type', 'jump_type', 'shop_link')


def _connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)


@lru_cache(maxsize=1)
def ensure_table():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS merchant_certification (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                wechat_id VARCHAR(128) NOT NULL,
                phone VARCHAR(32) NOT NULL,
                merchant_type VARCHAR(80) NOT NULL,
                jump_type VARCHAR(24) NOT NULL,
                shop_link VARCHAR(2048) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                created_by VARCHAR(128) NOT NULL DEFAULT '',
                updated_by VARCHAR(128) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_merchant_wechat (wechat_id),
                INDEX idx_merchant_phone (phone),
                INDEX idx_merchant_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        conn.commit()


def validate(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError('提交内容必须是对象')
    data = {}
    labels = dict(wechat_id='用户微信 ID', phone='用户手机号', merchant_type='商家类型', jump_type='店铺跳转方式', shop_link='店铺链接')
    for key in FIELDS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'请填写{labels[key]}')
        data[key] = value.strip()
    for key, limit in [('wechat_id', 128), ('phone', 32), ('merchant_type', 80), ('shop_link', 2048)]:
        if len(data[key]) > limit or any(ord(char) < 32 for char in data[key]):
            raise ValueError(f'{labels[key]}格式或长度不正确')
    phone = re.sub(r'[\s-]', '', data['phone'])
    if phone.startswith('+86'):
        phone = phone[3:]
    if not re.fullmatch(r'1[3-9]\d{9}', phone):
        raise ValueError('请输入有效的11位手机号，支持 +86 前缀')
    data['phone'] = phone
    if data['jump_type'] not in JUMP_TYPES:
        raise ValueError('请选择有效的店铺跳转方式')
    link = data['shop_link']
    try:
        parsed = urlsplit(link)
        if parsed.scheme.lower() in {'javascript', 'data', 'vbscript', 'file'}:
            raise ValueError('店铺链接格式不正确')
        if data['jump_type'] == 'web' and (parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or any(char.isspace() for char in link)):
            raise ValueError('网页链接须填写完整的 http:// 或 https:// 地址')
    except ValueError:
        raise ValueError('店铺链接格式不正确；网页链接须填写完整的 http:// 或 https:// 地址') from None
    return data


def _serialize(row):
    return {key: value.isoformat(sep=' ', timespec='seconds') if hasattr(value, 'isoformat') else value for key, value in row.items()}


def list_certifications(query='', status='', page=1, page_size=20):
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    if status and status not in {'active', 'disabled'}:
        raise ValueError('认证状态不正确')
    if len(query) > 128:
        raise ValueError('搜索内容过长')
    where, args = [], []
    if query.strip():
        where.append('(wechat_id LIKE %s OR phone LIKE %s OR merchant_type LIKE %s)')
        args.extend([f'%{query.strip()}%'] * 3)
    if status:
        where.append('status=%s')
        args.append(status)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    ensure_table()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM merchant_certification' + clause, args)
            total = cur.fetchone()['n']
            cur.execute('SELECT * FROM merchant_certification' + clause + ' ORDER BY id DESC LIMIT %s OFFSET %s', [*args, page_size, (page - 1) * page_size])
            rows = cur.fetchall()
    return {'ok': True, 'items': [_serialize(row) for row in rows], 'total': total, 'page': page, 'page_size': page_size}


def search_wechat_users(query=''):
    query = query.strip()
    if len(query) > 128:
        raise ValueError('搜索内容过长')
    if not query:
        return {'ok': True, 'items': [], 'has_more': False}
    escaped = query.replace('!', '!!').replace('%', '!%').replace('_', '!_')
    pattern = f'%{escaped}%'
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id,nickname,openid,phone_number,last_login_at
                FROM miniprogram_user WHERE status='active' AND
                (nickname LIKE %s ESCAPE '!' OR openid LIKE %s ESCAPE '!' OR phone_number LIKE %s ESCAPE '!')
                ORDER BY CASE WHEN nickname=%s THEN 0 ELSE 1 END,last_login_at DESC,id DESC LIMIT 21""",
                (pattern, pattern, pattern, query))
            rows = cur.fetchall()
    return {'ok': True, 'items': [_serialize(row) for row in rows[:20]], 'has_more': len(rows) > 20}


def save_certification(payload, *, actor='', record_id=None):
    data = validate(payload)
    selected_user_id = payload.get('selected_user_id')
    if selected_user_id is not None and (not isinstance(selected_user_id, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', selected_user_id)):
        raise ValueError('所选用户编号不正确，请重新搜索并选择')
    ensure_table()
    with _connect() as conn:
        with conn.cursor() as cur:
            if selected_user_id:
                cur.execute("SELECT openid FROM miniprogram_user WHERE id=%s AND status='active' FOR UPDATE", (selected_user_id,))
                user = cur.fetchone()
                if not user or user['openid'] != data['wechat_id']:
                    raise ValueError('所选微信用户已变化，请重新搜索并选择')
            if record_id is None:
                cur.execute('''INSERT INTO merchant_certification
                    (wechat_id,phone,merchant_type,jump_type,shop_link,created_by,updated_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)''', [*(data[key] for key in FIELDS), actor[:128], actor[:128]])
                record_id = cur.lastrowid
            else:
                cur.execute('SELECT id FROM merchant_certification WHERE id=%s FOR UPDATE', (record_id,))
                if not cur.fetchone():
                    raise KeyError('商家认证记录不存在')
                cur.execute('''UPDATE merchant_certification SET wechat_id=%s,phone=%s,merchant_type=%s,
                    jump_type=%s,shop_link=%s,updated_by=%s WHERE id=%s''', [*(data[key] for key in FIELDS), actor[:128], record_id])
            cur.execute('SELECT * FROM merchant_certification WHERE id=%s', (record_id,))
            row = cur.fetchone()
        conn.commit()
    return {'ok': True, 'item': _serialize(row)}


def set_status(record_id, status, *, actor=''):
    if not isinstance(status, str) or status not in {'active', 'disabled'}:
        raise ValueError('认证状态不正确')
    ensure_table()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM merchant_certification WHERE id=%s FOR UPDATE', (record_id,))
            if not cur.fetchone():
                raise KeyError('商家认证记录不存在')
            cur.execute('UPDATE merchant_certification SET status=%s,updated_by=%s WHERE id=%s', (status, actor[:128], record_id))
        conn.commit()
    return {'ok': True, 'id': record_id, 'status': status}
