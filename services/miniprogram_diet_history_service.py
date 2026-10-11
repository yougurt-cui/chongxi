"""Confirmed pet feeding records and bounded associations with private chat topics."""
from __future__ import annotations

import re
import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pymysql
from app_config import get_mysql_config

TABLE = 'miniprogram_pet_diet_history'
SHANGHAI = timezone(timedelta(hours=8))
ASSOCIATION_NOTE = '仅表示吃粮记录期间咨询过的话题，不代表粮食导致健康问题；未记录的日期不推断。'
TOPICS = (
    ('digestive', '排便与肠胃', re.compile(r'软便|腹泻|拉稀|便秘|呕吐|便血|消化|肠胃')),
    ('skin', '皮肤与毛发', re.compile(r'黑下巴|掉毛|脱毛|瘙痒|皮肤|美毛')),
    ('weight', '体重与食欲', re.compile(r'体重|肥胖|长肉|食欲|不吃|挑食|减重')),
    ('urinary', '泌尿与饮水', re.compile(r'尿血|血尿|尿闭|排尿|泌尿|饮水')),
    ('allergy', '过敏与耐受', re.compile(r'过敏|不耐受|低敏')),
)


def _connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def init_diet_history_table():
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
                id CHAR(32) NOT NULL, user_id VARCHAR(128) NOT NULL, pet_id CHAR(32) NOT NULL,
                brand VARCHAR(128) NOT NULL, product VARCHAR(512) NOT NULL,
                product_id BIGINT NULL, formula_id BIGINT NULL,
                started_at DATETIME NULL, ended_at DATETIME NULL, observed_at DATETIME NOT NULL,
                source VARCHAR(32) NOT NULL, is_current TINYINT NOT NULL DEFAULT 0,
                note VARCHAR(1000) NULL, status VARCHAR(16) NOT NULL DEFAULT 'active',
                PRIMARY KEY(id), KEY idx_diet_pet(user_id,pet_id,status,is_current,observed_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
        conn.commit()


def _food(pet):
    return {k: pet.get('food_' + k) for k in ('brand', 'product', 'product_id', 'formula_id')}


def _insert(cursor, user_id, pet_id, food, *, source, current, started=None, ended=None, observed=None, note='', record_id=None):
    record_id = record_id or uuid.uuid4().hex
    cursor.execute(f'''INSERT INTO {TABLE}
        (id,user_id,pet_id,brand,product,product_id,formula_id,started_at,ended_at,observed_at,source,is_current,note)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=id''',
        (record_id, user_id, pet_id, food.get('brand') or '', food.get('product') or '',
         food.get('product_id'), food.get('formula_id'), started, ended, observed or _now(), source, int(current), note))
    return record_id


def record_profile_diet(cursor, user_id, pet_id, before, after, now=None):
    """Caller locks the pet. Feeding history and profile changes commit atomically."""
    now = now or _now()
    if isinstance(now, str):
        now = datetime.fromisoformat(now)
    old, new = _food(before), _food(after)
    cursor.execute(f"SELECT * FROM {TABLE} WHERE user_id=%s AND pet_id=%s AND status='active' AND is_current=1 FOR UPDATE", (user_id, pet_id))
    current = cursor.fetchone()
    changed = old != new
    if not changed and current:
        return
    if current:
        cursor.execute(f'UPDATE {TABLE} SET ended_at=%s,is_current=0 WHERE id=%s', (now, current['id']))
    elif changed and (old['brand'] or old['product']):
        # Old profiles have no proven feeding dates; never invent them from updated_at.
        _insert(cursor, user_id, pet_id, old, source='profile_snapshot', current=False, ended=now, observed=now)
    if new['brand'] or new['product']:
        _insert(cursor, user_id, pet_id, new, source='profile_confirmed', current=True, observed=now)


def _owned_pet(cursor, user_id, pet_id, lock=False):
    cursor.execute("SELECT * FROM miniprogram_cat_profile WHERE id=%s AND user_id=%s AND status='active'" + (' FOR UPDATE' if lock else ''), (pet_id, user_id))
    row = cursor.fetchone()
    if not row:
        raise LookupError('宠物档案不存在')
    return row


def _date(value, name):
    if not value:
        return None
    try:
        date = datetime.strptime(str(value), '%Y-%m-%d').replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise ValueError(name + '须为 YYYY-MM-DD') from exc
    if date.date() > datetime.now(SHANGHAI).date():
        raise ValueError(name + '不能晚于今天')
    return date.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_history(payload):
    brand, product = str(payload.get('brand') or '').strip(), str(payload.get('product') or '').strip()
    if not brand or not product or len(brand) > 128 or len(product) > 512:
        raise ValueError('请填写品牌（128字以内）和具体产品（512字以内）')
    start, end = _date(payload.get('started_on'), '开始日期'), _date(payload.get('ended_on'), '结束日期')
    if start and end and start > end:
        raise ValueError('结束日期不能早于开始日期')
    note = str(payload.get('note') or '').strip()
    if len(note) > 1000:
        raise ValueError('备注不能超过1000字')
    return {'brand': brand, 'product': product, 'started': start,
            'ended': end + timedelta(days=1) if end else None, 'note': note}


def add_diet_history(user_id, pet_id, payload):
    data = normalize_history(payload)
    legacy_id = str(payload.get('legacy_record_id') or '').strip()
    if len(legacy_id) > 128:
        raise ValueError('旧记录标识过长')
    replay_id = hashlib.sha256(f'legacy:{user_id}:{pet_id}:{legacy_id}'.encode()).hexdigest()[:32] if legacy_id else None
    init_diet_history_table()
    with _connect() as conn:
        with conn.cursor() as cursor:
            _owned_pet(cursor, user_id, pet_id, lock=True)
            record_id = _insert(cursor, user_id, pet_id, data, source='user_history', current=False,
                                started=data['started'], ended=data['ended'], note=data['note'], record_id=replay_id)
        conn.commit()
    return {'ok': True, 'id': record_id}


def delete_diet_history(user_id, pet_id, record_id):
    init_diet_history_table()
    with _connect() as conn:
        with conn.cursor() as cursor:
            _owned_pet(cursor, user_id, pet_id, lock=True)
            cursor.execute(f"SELECT * FROM {TABLE} WHERE id=%s AND user_id=%s AND pet_id=%s AND status='active' FOR UPDATE", (record_id, user_id, pet_id))
            row = cursor.fetchone()
            if not row:
                raise LookupError('吃粮记录不存在')
            if row['is_current']:
                raise ValueError('当前口粮请在口粮清单中修改')
            cursor.execute(f"UPDATE {TABLE} SET status='deleted' WHERE id=%s", (record_id,))
        conn.commit()
    return {'ok': True, 'id': record_id}


def _display_date(value):
    if not value:
        return ''
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc).astimezone(SHANGHAI).strftime('%Y-%m-%d')


def health_associations(messages):
    # Count conversations per topic so short answers and repeated turns do not inflate frequency.
    topics = defaultdict(dict)
    for message in messages:
        text = message.get('content') or ''
        for code, label, pattern in TOPICS:
            if pattern.search(text):
                topics[code][message['conversation_id']] = {
                    'conversation_id': message['conversation_id'], 'message_id': message['id'],
                    'excerpt': text[:160], 'date': _display_date(message['created_at']), 'label': label,
                }
    return sorted([{'code': code, 'label': next(iter(rows.values()))['label'], 'conversation_count': len(rows),
                    'samples': list(rows.values())[:3]} for code, rows in topics.items()],
                  key=lambda x: (-x['conversation_count'], x['code']))


def list_diet_history(user_id, pet_id, *, limit=50, include_health=True):
    limit = max(1, min(int(limit or 50), 100))
    init_diet_history_table()
    with _connect() as conn:
        with conn.cursor() as cursor:
            pet = _owned_pet(cursor, user_id, pet_id, lock=True)
            # Seed current food only from the observation time; unknown past remains unknown.
            record_profile_diet(cursor, user_id, pet_id, pet, pet)
            cursor.execute(f"SELECT * FROM {TABLE} WHERE user_id=%s AND pet_id=%s AND status='active' ORDER BY is_current DESC,COALESCE(ended_at,started_at,observed_at) DESC,id LIMIT %s", (user_id, pet_id, limit + 1))
            rows = list(cursor.fetchall())
        conn.commit()
        items = []
        for row in rows[:limit]:
            topics, truncated, unavailable = [], False, False
            # Manual history needs both dates. A current observation can have an open end.
            lower = row['started_at'] or (row['observed_at'] if row['source'] == 'profile_confirmed' else None)
            bounded = lower is not None and (row['ended_at'] is not None or row['is_current'])
            if include_health and bounded:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute('''SELECT m.id,m.conversation_id,m.content,m.created_at
                            FROM miniprogram_chat_message m JOIN miniprogram_chat_conversation c ON c.id=m.conversation_id
                            WHERE c.user_id=%s AND c.pet_id=%s AND c.status='active'
                            AND m.user_id=%s AND m.status='active' AND m.role='user'
                            AND m.created_at >= %s AND (%s IS NULL OR m.created_at < %s)
                            ORDER BY m.created_at DESC,m.id LIMIT 1001''',
                            (user_id, pet_id, user_id, lower, row['ended_at'], row['ended_at']))
                        messages = list(cursor.fetchall())
                    topics = health_associations(messages[:1000])
                    truncated = len(messages) > 1000
                except pymysql.MySQLError as exc:
                    if exc.args[0] != 1146:
                        raise
                    unavailable = True
            items.append({'id': row['id'], 'brand': row['brand'], 'product': row['product'],
                          'product_id': row['product_id'], 'formula_id': row['formula_id'],
                          'is_current': bool(row['is_current']), 'source': row['source'], 'note': row['note'] or '',
                          'started_on': _display_date(row['started_at']),
                          'ended_on': _display_date(row['ended_at'] - timedelta(seconds=1)) if row['ended_at'] else '',
                          'observed_on': _display_date(row['observed_at']),
                          'health_topics': topics, 'association_available': bounded and not unavailable,
                          'association_truncated': truncated})
    return {'ok': True, 'items': items, 'has_more': len(rows) > limit, 'association_note': ASSOCIATION_NOTE}


def compact_diet_context(items):
    """Keep source IDs and topic counts; avoid repeating raw chat excerpts in model context."""
    return [{**{key: item.get(key) for key in ('id', 'brand', 'product', 'formula_id', 'is_current', 'source',
                                             'started_on', 'ended_on', 'observed_on', 'note', 'association_available', 'association_truncated')},
             'health_topics': [{key: topic[key] for key in ('code', 'label', 'conversation_count')} for topic in item.get('health_topics', [])]}
            for item in items]
