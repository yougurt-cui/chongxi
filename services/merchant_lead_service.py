"""Durable, bounded merchant enquiries; no contact data is written to logs."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

CATEGORIES = ('宠物食品', '宠物用品', '宠物服务', '其他')


class TooManyLeads(ValueError):
    pass


def _connect():
    path = Path(os.environ.get('MERCHANT_LEADS_DB') or
                Path(__file__).resolve().parents[1] / 'var' / 'merchant-leads.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS merchant_leads (
        id TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
        payload TEXT NOT NULL, fingerprint TEXT NOT NULL, client_hash TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS leads_created ON merchant_leads(created_at)')
    db.execute('CREATE INDEX IF NOT EXISTS leads_fingerprint ON merchant_leads(fingerprint, created_at)')
    db.execute('CREATE INDEX IF NOT EXISTS leads_client ON merchant_leads(client_hash, created_at)')
    db.commit()
    return db


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('请填写完整的合作信息')
    if payload.get('consent') is not True:
        raise ValueError('请同意我们为本次合作联系您')
    result = {}
    for key, label, limit, required in (
        ('company', '商家名称', 100, True), ('contact', '联系人', 50, True),
        ('phone', '手机号', 20, True), ('category', '经营品类', 20, True),
        ('message', '合作需求', 1000, False),
    ):
        value = payload.get(key, '')
        if not isinstance(value, str):
            raise ValueError(label + '格式不正确')
        value = value.strip()
        if (required and not value) or len(value) > limit:
            raise ValueError('请检查' + label + '（最多 %s 字）' % limit)
        result[key] = value
    if not re.fullmatch(r'1[3-9][0-9]{9}', result['phone']):
        raise ValueError('请输入正确的 11 位大陆手机号')
    if result['category'] not in CATEGORIES:
        raise ValueError('请选择经营品类')
    result['consent'] = True
    result['consent_version'] = 'merchant-contact-v1'
    result['source'] = 'merchant-cooperation'
    return result


def save_lead(payload, client_address):
    data = validate(payload)
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
    client_hash = hashlib.sha256(str(client_address).encode()).hexdigest()
    now = int(time.time())
    db = _connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT id FROM merchant_leads WHERE fingerprint=? AND created_at>?',
                              (fingerprint, now - 86400)).fetchone()
        if previous:
            return previous['id']
        count = db.execute('SELECT COUNT(*) FROM merchant_leads WHERE client_hash=? AND created_at>?',
                           (client_hash, now - 3600)).fetchone()[0]
        if count >= 10:
            raise TooManyLeads('提交较频繁，请稍后再试')
        lead_id = uuid.uuid4().hex
        db.execute('INSERT INTO merchant_leads VALUES (?, ?, ?, ?, ?)',
                   (lead_id, now, serialized, fingerprint, client_hash))
        db.commit()
        return lead_id
    finally:
        db.close()


def list_leads(limit=100):
    db = _connect()
    try:
        return [dict(id=row['id'], created_at=row['created_at'], **json.loads(row['payload']))
                for row in db.execute('SELECT id, created_at, payload FROM merchant_leads ORDER BY created_at DESC LIMIT ?', (limit,))]
    finally:
        db.close()
