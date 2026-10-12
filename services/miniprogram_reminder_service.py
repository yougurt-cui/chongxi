"""Account-owned reminders, recurrence and transactional completion history.

This is the reminder repository. Notification delivery is a separate capability;
creating an in-app reminder never claims that a push notification was sent.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pymysql
from app_config import get_mysql_config
from services.miniprogram_cat_profile_service import get_cat_profile

TABLE = 'miniprogram_reminder'
HISTORY_TABLE = 'miniprogram_reminder_history'
SHANGHAI = timezone(timedelta(hours=8))
REPEATS = {'none', 'daily', 'weekly', 'biweekly', 'monthly', 'yearly'}
TYPES = {'birthday', 'pump', 'litter', 'bath', 'checkup', 'food', 'internal', 'external', 'vaccine', 'groom', 'drum', 'waste', 'fountain', 'refill', 'custom'}


def _connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor)


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def init_reminder_tables():
    with _connect() as conn:
        with conn.cursor() as c:
            c.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
                id CHAR(32) NOT NULL,user_id VARCHAR(128) NOT NULL,pet_id CHAR(32) NULL,
                title VARCHAR(160) NOT NULL,reminder_type VARCHAR(32) NOT NULL,due_at DATETIME NOT NULL,
                repeat_rule VARCHAR(16) NOT NULL,anchor_day INT NOT NULL,anchor_month INT NOT NULL,
                note VARCHAR(1000) NOT NULL DEFAULT '',source VARCHAR(32) NOT NULL DEFAULT 'manual',
                status VARCHAR(16) NOT NULL DEFAULT 'active',revision BIGINT NOT NULL DEFAULT 1,idempotency_key CHAR(64) NOT NULL,
                request_json JSON NOT NULL,created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,
                PRIMARY KEY(id),UNIQUE KEY uq_reminder_request(user_id,idempotency_key),
                KEY idx_reminder_owner(user_id,status,due_at),KEY idx_reminder_pet(user_id,pet_id,source)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            c.execute(f'''CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
                id VARCHAR(80) NOT NULL,user_id VARCHAR(128) NOT NULL,reminder_id CHAR(32) NOT NULL,
                snapshot_json JSON NOT NULL,completed_at DATETIME NOT NULL,
                PRIMARY KEY(id),KEY idx_reminder_history(user_id,completed_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')
            c.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name=%s AND column_name='revision'", (TABLE,))
            if not c.fetchone():
                try:
                    c.execute(f'ALTER TABLE {TABLE} ADD COLUMN revision BIGINT NOT NULL DEFAULT 1')
                except pymysql.MySQLError as error:
                    if error.args[0] != 1060:
                        raise
        conn.commit()


def _utc_naive(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def normalize(payload, *, now=None):
    title = str(payload.get('title') or '').strip()
    if not 1 <= len(title) <= 40:
        raise ValueError('请填写 1～40 字的提醒内容')
    repeat = payload.get('repeat') or 'none'
    kind = payload.get('type') or 'custom'
    if repeat not in REPEATS or kind not in TYPES:
        raise ValueError('提醒类型或重复方式无效')
    try:
        if payload.get('dueAt') is not None:
            due = datetime.fromtimestamp(float(payload['dueAt']) / 1000, timezone.utc)
        elif payload.get('due_at'):
            due = datetime.fromisoformat(str(payload['due_at']).replace('Z', '+00:00'))
            if due.tzinfo is None:
                due = due.replace(tzinfo=SHANGHAI)
        else:
            due = datetime.strptime(str(payload.get('date') or '') + ' ' + str(payload.get('time') or ''), '%Y-%m-%d %H:%M').replace(tzinfo=SHANGHAI)
        due = due.astimezone(timezone.utc).replace(microsecond=0)
    except (TypeError, ValueError, OverflowError, OSError):
        raise ValueError('请提供有效的提醒日期和时间') from None
    if due <= (now or _now()):
        raise ValueError('提醒时间已经过去，请提供未来时间')
    if due.year > 2100:
        raise ValueError('提醒日期过远，请核对年份')
    note = str(payload.get('note') or '').strip()
    if len(note) > 500:
        raise ValueError('提醒备注不能超过500字')
    return {'title': title, 'type': kind, 'repeat': repeat, 'due_at': due.isoformat(),
            'pet_id': str(payload.get('pet_id') or payload.get('petId') or ''), 'note': note}


def next_occurrence(due, repeat, now, anchor_day=None, anchor_month=None):
    if repeat == 'none':
        return None
    local, today = _aware(due).astimezone(SHANGHAI), _aware(now).astimezone(SHANGHAI)
    if repeat in {'daily', 'weekly', 'biweekly'}:
        days = {'daily': 1, 'weekly': 7, 'biweekly': 14}[repeat]
        cycles = max(1, int((today - local).total_seconds() // (days * 86400)) + 1)
        return (local + timedelta(days=days * cycles)).astimezone(timezone.utc)
    if repeat not in {'monthly', 'yearly'}:
        raise ValueError('重复方式无效')
    day, month = anchor_day or local.day, anchor_month or local.month
    if repeat == 'monthly':
        jump = max(1, (today.year - local.year) * 12 + today.month - local.month)
        index = local.year * 12 + local.month - 1 + jump
        year, target_month = divmod(index, 12)
        target_month += 1
    else:
        year, target_month = max(local.year + 1, today.year), month
    result = local.replace(year=year, month=target_month, day=min(day, calendar.monthrange(year, target_month)[1]))
    if result <= today:
        if repeat == 'yearly':
            year += 1
        else:
            index = year * 12 + target_month
            year, target_month = divmod(index, 12)
            target_month += 1
        result = local.replace(year=year, month=target_month, day=min(day, calendar.monthrange(year, target_month)[1]))
    return result.astimezone(timezone.utc)


def serialize(row):
    request = _json(row.get('request_json'))
    due = _aware(row['due_at'])
    return {'id': row['id'], 'title': row['title'], 'type': row['reminder_type'], 'dueAt': int(due.timestamp() * 1000),
            'repeat': row['repeat_rule'], 'petId': row.get('pet_id') or '', 'petName': row.get('pet_name') or '',
            'note': row.get('note') or '', 'source': row.get('source') or 'manual', 'cloud': True,
            'birthday': request.get('birthday') or '', 'anchorDay': row['anchor_day'], 'anchorMonth': row['anchor_month'] - 1,
            'revision': row.get('revision', 1),
            'createdAt': int(_aware(row['created_at']).timestamp() * 1000), 'updatedAt': int(_aware(row['updated_at']).timestamp() * 1000)}


def list_reminders(user_id):
    init_reminder_tables()
    with _connect() as conn:
        with conn.cursor() as c:
            c.execute(f'''SELECT r.*,p.name AS pet_name FROM {TABLE} r LEFT JOIN miniprogram_cat_profile p
                ON p.id=r.pet_id AND p.user_id=r.user_id AND p.status='active'
                WHERE r.user_id=%s AND r.status='active' ORDER BY r.due_at,r.id''', (user_id,))
            items = [serialize(row) for row in c.fetchall()]
            c.execute(f'SELECT snapshot_json,completed_at,id FROM {HISTORY_TABLE} WHERE user_id=%s ORDER BY completed_at DESC LIMIT 500', (user_id,))
            history = [{**_json(row['snapshot_json']), 'id': row['id'], 'completedAt': int(_aware(row['completed_at']).timestamp() * 1000), 'cloud': True} for row in c.fetchall()]
    return {'ok': True, 'items': items, 'history': history, 'notification_channel': 'in_app', 'timezone': 'Asia/Shanghai'}


def create_reminder(user_id, payload, *, source='manual'):
    raw_key = str(payload.get('idempotency_key') or payload.get('legacy_id') or '').strip()
    if not raw_key or len(raw_key) > 200:
        raise ValueError('请提供有效的 idempotency_key，避免重复保存')
    key = hashlib.sha256(raw_key.encode()).hexdigest()
    init_reminder_tables()
    # A retry may arrive after the original time; check the saved operation first.
    with _connect() as conn:
        with conn.cursor() as c:
            c.execute(f'SELECT r.*,p.name AS pet_name FROM {TABLE} r LEFT JOIN miniprogram_cat_profile p ON p.id=r.pet_id AND p.user_id=r.user_id WHERE r.user_id=%s AND r.idempotency_key=%s', (user_id, key))
            previous = c.fetchone()
            if previous and previous['status'] != 'deleted':
                original = _json(previous['request_json'])
                clean = normalize(payload, now=datetime(1970, 1, 1, tzinfo=timezone.utc))
                if any(clean.get(k) != original.get(k) for k in clean):
                    raise ValueError('同一保存标识对应了不同提醒，请重新提交')
                return {'ok': True, 'item': serialize(previous), 'replayed': True}
    data = normalize(payload, now=datetime(1970, 1, 1, tzinfo=timezone.utc) if payload.get('legacy_id') else None)
    pet = get_cat_profile(user_id, data['pet_id']) if data['pet_id'] else None
    due = datetime.fromisoformat(data['due_at'])
    local, now = due.astimezone(SHANGHAI), _now()
    reminder_id = previous['id'] if previous else uuid.uuid4().hex
    with _connect() as conn:
        with conn.cursor() as c:
            c.execute(f'''INSERT INTO {TABLE} (id,user_id,pet_id,title,reminder_type,due_at,repeat_rule,anchor_day,anchor_month,note,source,idempotency_key,request_json,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE status=IF(status='deleted','active',status),updated_at=updated_at''',
                (reminder_id, user_id, data['pet_id'] or None, data['title'], data['type'], _utc_naive(due), data['repeat'],
                 local.day, local.month, data['note'], source, key, json.dumps(data, ensure_ascii=False), _utc_naive(now), _utc_naive(now)))
            c.execute(f'SELECT * FROM {TABLE} WHERE user_id=%s AND idempotency_key=%s', (user_id, key))
            row = c.fetchone()
            if _json(row['request_json']) != data:
                raise ValueError('保存标识冲突，请重新提交')
        conn.commit()
    row['pet_name'] = (pet or {}).get('name') or ''
    return {'ok': True, 'item': serialize(row), 'notification_channel': 'in_app'}


def update_reminder(user_id, reminder_id, payload):
    data = normalize(payload, now=datetime(1970, 1, 1, tzinfo=timezone.utc))
    pet = get_cat_profile(user_id, data['pet_id']) if data['pet_id'] else None
    due = datetime.fromisoformat(data['due_at']); local = due.astimezone(SHANGHAI)
    init_reminder_tables()
    with _connect() as conn:
        with conn.cursor() as c:
            c.execute(f'SELECT * FROM {TABLE} WHERE id=%s AND user_id=%s AND status=\'active\' FOR UPDATE', (reminder_id, user_id))
            old = c.fetchone()
            if not old:
                raise LookupError('提醒不存在或已结束')
            if payload.get('expected_revision') is not None and payload['expected_revision'] != old['revision']:
                raise ValueError('提醒已在其他地方更新，请重新打开后再修改')
            if due <= _now() and old['due_at'] != _utc_naive(due):
                raise ValueError('请选择未来的提醒时间')
            c.execute(f'''UPDATE {TABLE} SET pet_id=%s,title=%s,reminder_type=%s,due_at=%s,repeat_rule=%s,anchor_day=%s,anchor_month=%s,note=%s,updated_at=%s,revision=revision+1
                WHERE id=%s AND user_id=%s''', (data['pet_id'] or None, data['title'], data['type'], _utc_naive(due), data['repeat'],
                old['anchor_day'] if old['due_at'] == _utc_naive(due) else local.day,
                old['anchor_month'] if old['due_at'] == _utc_naive(due) else local.month,
                data['note'], _utc_naive(_now()), reminder_id, user_id))
            c.execute(f'SELECT * FROM {TABLE} WHERE id=%s AND user_id=%s', (reminder_id, user_id)); row = c.fetchone()
        conn.commit()
    row['pet_name'] = (pet or {}).get('name') or ''
    return {'ok': True, 'item': serialize(row)}


def delete_reminder(user_id, reminder_id):
    init_reminder_tables()
    with _connect() as conn:
        with conn.cursor() as c:
            c.execute(f'UPDATE {TABLE} SET status=\'deleted\',updated_at=%s,revision=revision+1 WHERE id=%s AND user_id=%s AND status!=\'deleted\'', (_utc_naive(_now()), reminder_id, user_id))
            if not c.rowcount:
                c.execute(f'SELECT id FROM {TABLE} WHERE id=%s AND user_id=%s', (reminder_id, user_id))
                if not c.fetchone():
                    raise LookupError('提醒不存在')
        conn.commit()
    return {'ok': True}


def complete_reminder(user_id, reminder_id, payload):
    try:
        expected = int(payload['expected_due_at'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('请提供当前提醒时间 expected_due_at') from None
    init_reminder_tables(); now = _now()
    with _connect() as conn:
        with conn.cursor() as c:
            c.execute(f'SELECT r.*,p.name AS pet_name FROM {TABLE} r LEFT JOIN miniprogram_cat_profile p ON p.id=r.pet_id AND p.user_id=r.user_id WHERE r.id=%s AND r.user_id=%s FOR UPDATE', (reminder_id, user_id))
            row = c.fetchone()
            if not row:
                raise LookupError('提醒不存在')
            history_id = reminder_id + '-' + str(expected)
            c.execute(f'SELECT id FROM {HISTORY_TABLE} WHERE id=%s AND user_id=%s', (history_id, user_id))
            if c.fetchone():
                return {'ok': True, 'replayed': True}
            if row['status'] != 'active' or int(_aware(row['due_at']).timestamp() * 1000) != expected:
                raise ValueError('提醒已更新，请刷新后再完成')
            snapshot = serialize(row)
            snapshot['reminderId'] = reminder_id
            c.execute(f'INSERT INTO {HISTORY_TABLE} (id,user_id,reminder_id,snapshot_json,completed_at) VALUES (%s,%s,%s,%s,%s)',
                      (history_id, user_id, reminder_id, json.dumps(snapshot, ensure_ascii=False), _utc_naive(now)))
            next_due = next_occurrence(row['due_at'], row['repeat_rule'], now, row['anchor_day'], row['anchor_month'])
            c.execute(f'UPDATE {TABLE} SET status=%s,due_at=%s,updated_at=%s,revision=revision+1 WHERE id=%s AND user_id=%s',
                      ('active' if next_due else 'completed', _utc_naive(next_due) if next_due else row['due_at'], _utc_naive(now), reminder_id, user_id))
            row.update(due_at=_utc_naive(next_due) if next_due else row['due_at'], updated_at=_utc_naive(now), revision=row['revision'] + 1)
        conn.commit()
    return {'ok': True, 'item': serialize(row) if next_due else None,
            'history': {**snapshot, 'id': history_id, 'completedAt': int(now.timestamp() * 1000)}}


def sync_birthday_reminder(user_id, pet_id):
    pet = get_cat_profile(user_id, pet_id)
    birthday = str(pet.get('birthday') or '')[:10]
    init_reminder_tables(); now = _now()
    key = hashlib.sha256(('pet-birthday:' + pet_id).encode()).hexdigest()
    with _connect() as conn:
        with conn.cursor() as c:
            c.execute(f'SELECT * FROM {TABLE} WHERE user_id=%s AND idempotency_key=%s FOR UPDATE', (user_id, key))
            old = c.fetchone()
            if not birthday:
                if old:
                    c.execute(f'UPDATE {TABLE} SET status=\'deleted\',updated_at=%s WHERE id=%s', (_utc_naive(now), old['id']))
                conn.commit()
                return {'ok': True, 'item': None}
            if old and old['status'] == 'active' and _json(old['request_json']).get('birthday') == birthday:
                old['pet_name'] = pet.get('name') or ''
                return {'ok': True, 'item': serialize(old)}
            birth = datetime.strptime(birthday, '%Y-%m-%d')
            year = now.astimezone(SHANGHAI).year
            due = datetime(year, birth.month, min(birth.day, calendar.monthrange(year, birth.month)[1]), 9, tzinfo=SHANGHAI)
            if due <= now:
                due = next_occurrence(due, 'yearly', now, birth.day, birth.month)
            title = (pet.get('name') or '宠物') + '的生日'
            payload = {'title': title, 'type': 'birthday', 'pet_id': pet_id, 'repeat': 'yearly', 'due_at': due.isoformat(), 'note': '生日当天09:00，每年提醒', 'birthday': birthday}
            c.execute(f'''INSERT INTO {TABLE} (id,user_id,pet_id,title,reminder_type,due_at,repeat_rule,anchor_day,anchor_month,note,source,idempotency_key,request_json,created_at,updated_at)
                VALUES (%s,%s,%s,%s,'birthday',%s,'yearly',%s,%s,%s,'pet-birthday',%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE title=VALUES(title),due_at=VALUES(due_at),repeat_rule='yearly',anchor_day=VALUES(anchor_day),anchor_month=VALUES(anchor_month),request_json=VALUES(request_json),status='active',updated_at=VALUES(updated_at)''',
                (old['id'] if old else uuid.uuid4().hex, user_id, pet_id, title, _utc_naive(due), birth.day, birth.month, payload['note'], key,
                 json.dumps(payload, ensure_ascii=False), _utc_naive(now), _utc_naive(now)))
            c.execute(f'SELECT * FROM {TABLE} WHERE user_id=%s AND idempotency_key=%s', (user_id, key)); row = c.fetchone()
        conn.commit()
    row['pet_name'] = pet.get('name') or ''
    return {'ok': True, 'item': serialize(row)}
