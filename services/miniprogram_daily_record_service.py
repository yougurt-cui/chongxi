"""Owner-scoped absolute daily observations, shared by manual and reported devices."""
import json
from datetime import date, datetime, timedelta, timezone
from math import isfinite

from services.miniprogram_diet_history_service import _connect, _owned_pet

TABLE = 'miniprogram_pet_daily_record'
SHANGHAI = timezone(timedelta(hours=8))
COUNTS = {'food_count': '进食次数', 'water_count': '饮水次数', 'stool_count': '排便次数'}
AMOUNTS = {'water_ml': ('饮水量', 10000), 'food_g': ('进食量', 10000)}


def today():
    return datetime.now(SHANGHAI).date()


def normalize_record(payload, *, require_day=True):
    if not isinstance(payload, dict):
        raise ValueError('日常记录须为对象')
    result = {}
    if require_day or 'day' in payload:
        try:
            day = date.fromisoformat(str(payload.get('day')))
        except ValueError as error:
            raise ValueError('记录日期须为 YYYY-MM-DD') from error
        if str(day) != payload.get('day') or day > today():
            raise ValueError('记录日期须为 YYYY-MM-DD，且不能晚于今天')
        result['day'] = str(day)
    for key, label in COUNTS.items():
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == '':
            result[key] = None
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(label + '须为0～999的整数') from error
        if isinstance(value, bool) or not isfinite(parsed) or not parsed.is_integer() or not 0 <= parsed <= 999:
            raise ValueError(label + '须为0～999的整数')
        result[key] = int(parsed)
    for key, (label, maximum) in AMOUNTS.items():
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == '':
            result[key] = None
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(label + '格式不正确') from error
        if isinstance(value, bool) or not isfinite(parsed) or not 0 <= parsed <= maximum:
            raise ValueError(label + '数值超出记录范围')
        result[key] = parsed
    if 'stool_shape' in payload:
        value = payload['stool_shape']
        if value is not None and (not isinstance(value, str) or len(value) > 100):
            raise ValueError('便便形态须为100字以内文本')
        result['stool_shape'] = value or ''
    if 'complete_day' in payload:
        if not isinstance(payload['complete_day'], bool):
            raise ValueError('complete_day 须为布尔值')
        if payload['complete_day'] and result.get('day') == str(today()):
            raise ValueError('今天尚未结束，请按截至目前记录，不能标记完整自然日')
        result['complete_day'] = payload['complete_day']
    return result


def normalize_client_records(records):
    """Untrusted legacy snapshots remain ephemeral and never imply whole-day coverage."""
    result = {}
    for record in (records if isinstance(records, list) else [])[:31]:
        try:
            item = normalize_record(record)
        except ValueError:
            continue
        if not any(key in item for key in (*COUNTS, *AMOUNTS, 'stool_shape')):
            continue
        notes = record.get('litter_notes') or []
        item['stool_notes'] = [{'time': str(n.get('time') or '')[:5], 'count': n.get('count'),
                                'shape': str(n.get('shape') or '')[:60]}
                               for n in notes[:10] if isinstance(n, dict)] if isinstance(notes, list) else []
        item['source'] = 'client_snapshot'
        # Existing clients do not establish device identity or complete sampling.
        item['complete_day'] = False
        result[item['day']] = item
    return sorted(result.values(), key=lambda row: row['day'], reverse=True)[:7]


def init_daily_record_table():
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
                user_id VARCHAR(128) NOT NULL, pet_id CHAR(32) NOT NULL, day DATE NOT NULL,
                data_json JSON NOT NULL, updated_at DATETIME NOT NULL,
                PRIMARY KEY(user_id,pet_id,day)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        conn.commit()


def list_daily_records(user_id, pet_id):
    init_daily_record_table()
    with _connect() as conn:
        with conn.cursor() as cursor:
            _owned_pet(cursor, user_id, pet_id)
            cursor.execute(f'SELECT data_json FROM {TABLE} WHERE user_id=%s AND pet_id=%s AND day BETWEEN %s AND %s ORDER BY day DESC LIMIT 14',
                           (user_id, pet_id, today() - timedelta(days=13), today()))
            rows = cursor.fetchall()
    return {'ok': True, 'items': [json.loads(row['data_json']) if isinstance(row['data_json'], str)
                                  else row['data_json'] for row in rows]}


def save_daily_record(user_id, pet_id, payload):
    incoming = normalize_record(payload)
    if not any(key in incoming for key in (*COUNTS, *AMOUNTS, 'stool_shape')):
        raise ValueError('请至少填写一项次数、量值或便便形态')
    source = payload.get('source', 'manual')
    if not isinstance(source, str) or source not in {'manual', 'reported_device'}:
        raise ValueError('source 仅支持 manual/reported_device；设备数据由用户提供，不代表设备已接入')
    init_daily_record_table()
    with _connect() as conn:
        with conn.cursor() as cursor:
            # The pet lock serializes first insert as well as subsequent updates.
            _owned_pet(cursor, user_id, pet_id, lock=True)
            cursor.execute(f'SELECT data_json FROM {TABLE} WHERE user_id=%s AND pet_id=%s AND day=%s FOR UPDATE',
                           (user_id, pet_id, incoming['day']))
            row = cursor.fetchone()
            previous = (json.loads(row['data_json']) if isinstance(row['data_json'], str) else row['data_json']) if row else {}
            merged = {**previous, **incoming}
            sources = dict(previous.get('metric_sources') or {})
            for key in incoming:
                if key in (*COUNTS, *AMOUNTS, 'stool_shape'):
                    sources[key] = source
            merged.update(metric_sources=sources, source=source,
                          complete_day=incoming.get('complete_day', False),
                          updated_at=datetime.now(timezone.utc).isoformat())
            cursor.execute(f'''INSERT INTO {TABLE} (user_id,pet_id,day,data_json,updated_at)
                VALUES (%s,%s,%s,%s,UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE
                data_json=VALUES(data_json),updated_at=VALUES(updated_at)''',
                           (user_id, pet_id, incoming['day'], json.dumps(merged, ensure_ascii=False)))
        conn.commit()
    return {'ok': True, 'record': merged}
