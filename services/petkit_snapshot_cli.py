#!/usr/bin/env python3
"""PETKIT CN 设备快照采集 CLI（供 petkit_service 通过子进程调用）。

从 ~/WorkBuddy/.../petkit_water/petkit_water_sync.py 移植，并新增 --result-json
输出机器可读结果。**本文件必须用独立的 petkit venv（Python 3.13）运行**：

    /Users/yoghourt/.workbuddy/binaries/python/envs/petkit/bin/python services/petkit_snapshot_cli.py --list-only

原因：pypetkitapi 依赖 Python 3.11+ 的 enum.StrEnum，而 Flask 工程环境是 3.10。

职责与原脚本一致：只读拉取设备快照 → 脱敏 → 落 SQLite / 导出 JSON；
不做任何写设备操作。凭据读 ~/.petkit/env，session 缓存在 ~/.petkit/session.json
（复用 session，避免每次运行把手机 App 顶下线）。
"""
import argparse
import asyncio
import dataclasses
import datetime as dt
import enum
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path

import aiohttp
from pypetkitapi.client import PetKitClient
from pypetkitapi.containers import SessionInfo

SENSITIVE = ("password", "token", "secret", "authorization", "cookie", "access_key", "refresh", "phone", "email")
SENSITIVE_ALLOW = ("microphone",)  # settings.microphone 是麦克风开关，不是手机号
WATER_HINTS = ("water", "drink", "fountain", "filter", "pump", "level", "hydration", "水", "饮", "滤芯", "水泵")
FEEDER_HINTS = ("eat", "feed", "food", "bowl", "amount", "desiccant", "meal", "plan",
                "粮", "进食", "余粮", "喂", "干燥剂")
HINT_PRESETS = {"water": WATER_HINTS, "feeder": FEEDER_HINTS}
SNAPSHOT_SCHEMA_VERSION = "petkit-snapshot-v1"
PARSER_NAME = "pypetkitapi-entity-snapshot"
PARSER_VERSION = "1"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "petkit_water.sqlite3"
DEFAULT_ENV_FILE = Path(os.getenv("PETKIT_ENV_FILE") or (Path.home() / ".petkit" / "env"))
SESSION_CACHE = Path(os.getenv("PETKIT_SESSION_CACHE") or (Path.home() / ".petkit" / "session.json"))


def load_session_cache(path=SESSION_CACHE):
    """读取上次登录的 session，避免每次运行都重新登录把 App 顶下线。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not data.get("id") or not data.get("created_at"):
        return None
    payload = {
        "id": data["id"],
        "userId": str(data.get("user_id") or ""),
        "expiresIn": int(data.get("expires_in") or 0),
        "createdAt": data["created_at"],
        "region": data.get("region"),
    }
    try:
        return SessionInfo(**payload)
    except Exception:
        return SessionInfo(id=data["id"], user_id=data.get("user_id"),
                           expires_in=int(data.get("expires_in") or 0),
                           created_at=data["created_at"], region=data.get("region"))


def save_session_cache(session, path=SESSION_CACHE):
    """session 等价于登录凭据，落盘必须 600。"""
    if session is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": session.id,
        "user_id": session.user_id,
        "expires_in": session.expires_in,
        "region": session.region,
        "created_at": session.created_at,
        "refreshed_at": session.refreshed_at,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def load_env_file(path=DEFAULT_ENV_FILE):
    """Load KEY=VALUE lines into os.environ; real env vars take precedence."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def safe_convert(obj, depth=0, seen=None):
    """Best-effort JSON conversion. Avoid accidentally persisting credentials."""
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return '<binary omitted>'
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, enum.Enum):
        return safe_convert(obj.value, depth + 1, seen)
    if depth >= 12:
        return '<depth limit>'
    obj_id = id(obj)
    if obj_id in seen:
        return '<circular reference>'
    seen.add(obj_id)
    try:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            obj = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
        elif hasattr(obj, 'model_dump') and callable(obj.model_dump):
            obj = obj.model_dump(mode='json')
        elif not isinstance(obj, (Mapping, list, tuple, set)) and hasattr(obj, '__dict__'):
            obj = vars(obj)
        if isinstance(obj, Mapping):
            out = {}
            for k, v in obj.items():
                key = str(k)
                lowered = key.lower()
                if any(flag in lowered for flag in SENSITIVE) and not any(
                    allow in lowered for allow in SENSITIVE_ALLOW
                ):
                    out[key] = '<redacted>'
                else:
                    out[key] = safe_convert(v, depth + 1, seen)
            return out
        if isinstance(obj, (list, tuple, set)):
            return [safe_convert(v, depth + 1, seen) for v in obj]
        return str(obj)[:200]
    finally:
        seen.remove(obj_id)


def water_candidates(value, path='$', result=None, hints=WATER_HINTS):
    if result is None:
        result = []
    if isinstance(value, dict):
        for k, v in value.items():
            p = f'{path}.{k}'
            if any(h in k.lower() for h in hints) and not isinstance(v, (dict, list)):
                result.append((p, v))
            water_candidates(v, p, result, hints)
    elif isinstance(value, list):
        for i, v in enumerate(value[:100]):
            water_candidates(v, f'{path}[{i}]', result, hints)
    return result


def open_db(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute('''CREATE TABLE IF NOT EXISTS device_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at_utc TEXT NOT NULL,
        device_key TEXT NOT NULL,
        device_class TEXT NOT NULL,
        device_name TEXT,
        payload_sha256 TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        vendor TEXT NOT NULL DEFAULT 'petkit',
        device_model TEXT,
        source_time TEXT,
        schema_version TEXT NOT NULL DEFAULT 'petkit-snapshot-v1',
        parser_name TEXT NOT NULL DEFAULT 'pypetkitapi-entity-snapshot',
        parser_version TEXT NOT NULL DEFAULT '1',
        parse_status TEXT NOT NULL DEFAULT 'raw',
        parsed_at_utc TEXT
    )''')
    columns = {row[1] for row in db.execute("PRAGMA table_info(device_snapshots)")}
    additions = {
        "vendor": "TEXT NOT NULL DEFAULT 'petkit'",
        "device_model": "TEXT",
        "source_time": "TEXT",
        "schema_version": "TEXT NOT NULL DEFAULT 'petkit-snapshot-v1'",
        "parser_name": "TEXT NOT NULL DEFAULT 'pypetkitapi-entity-snapshot'",
        "parser_version": "TEXT NOT NULL DEFAULT '1'",
        "parse_status": "TEXT NOT NULL DEFAULT 'raw'",
        "parsed_at_utc": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE device_snapshots ADD COLUMN {name} {definition}")
    db.execute('CREATE INDEX IF NOT EXISTS idx_water_snapshots ON device_snapshots(device_key,captured_at_utc)')
    db.commit()
    return db


def write_result(result, path):
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(args):
    result = {"ok": False, "devices": [], "snapshots": [], "session_reused": False}
    load_env_file()
    username = os.getenv('PETKIT_USERNAME')
    password = os.getenv('PETKIT_PASSWORD')
    if not username or not password:
        raise RuntimeError(
            'Please set PETKIT_USERNAME and PETKIT_PASSWORD environment variables, '
            f'or put them in {DEFAULT_ENV_FILE}'
        )
    region = os.getenv('PETKIT_REGION', 'CN')
    timezone = os.getenv('PETKIT_TIMEZONE', 'Asia/Shanghai')
    db = None if args.list_only else open_db(args.db)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            client = PetKitClient(username=username, password=password,
                                  region=region, timezone=timezone, session=session)
            # 优先复用缓存 session：不再每次 login，避免把手机 App 顶下线
            cached = None if args.force_login else load_session_cache()
            if cached is not None:
                client._session = cached
                await client._get_base_url()
                result["session_reused"] = True
                print(f'Session reused from {SESSION_CACHE} (no login)')
            await client.get_devices_data()
            save_session_cache(client._session)
            entities = client.petkit_entities
            print('Devices/entities visible in account (names and types):')
            for key, entity in entities.items():
                device = {
                    "key": str(key),
                    "device_class": type(entity).__name__,
                    "name": str(getattr(entity, "name", "")),
                }
                result["devices"].append(device)
                print(f' - key={device["key"]} class={device["device_class"]} name={device["name"]!r}')

            if args.list_only:
                result["ok"] = True
                write_result(result, args.result_json)
                return 0

            candidates = []
            for key, entity in entities.items():
                class_name = type(entity).__name__
                name = str(getattr(entity, 'name', ''))
                is_fountain = any(h in (class_name + ' ' + name).lower()
                                  for h in ('fountain', 'water', 'drink', '饮水', '饮水机'))
                selected = (str(key) == args.device_id) if args.device_id else is_fountain
                if selected:
                    candidates.append((key, entity))

            if not candidates:
                result["error"] = "no device selected; pass --device-id from the device list"
                write_result(result, args.result_json)
                print('\nNo fountain auto-identified. Run with --device-id <key> from list above.')
                return 2

            stamp = dt.datetime.now(dt.timezone.utc).isoformat()
            hints = HINT_PRESETS.get(args.hints) or tuple(
                h.strip() for h in args.hints.split(',') if h.strip()
            )
            hint_label = args.hints if args.hints in HINT_PRESETS else 'custom'
            for key, entity in candidates:
                payload = safe_convert(entity)
                raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
                device_nfo = payload.get("device_nfo") or {}
                source_time = payload.get("update_at") or payload.get("sync_time")
                db.execute('''INSERT INTO device_snapshots
                    (captured_at_utc, device_key, device_class, device_name, payload_sha256, raw_json,
                     vendor, device_model, source_time, schema_version, parser_name, parser_version,
                     parse_status, parsed_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (stamp, str(key), type(entity).__name__, str(getattr(entity, 'name', '')), digest, raw,
                     "petkit", device_nfo.get("modele_name"), source_time, SNAPSHOT_SCHEMA_VERSION,
                     PARSER_NAME, PARSER_VERSION, "raw", None))
                snapshot_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                hits = water_candidates(payload, hints=hints)
                snapshot = {
                    "device_key": str(key),
                    "device_class": type(entity).__name__,
                    "device_name": str(getattr(entity, 'name', '')),
                    "captured_at_utc": stamp,
                    "payload_sha256": digest,
                    "snapshot_id": snapshot_id,
                    "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "parser_name": PARSER_NAME,
                    "parser_version": PARSER_VERSION,
                    "source_time": source_time,
                    "candidates": [[p, str(v)] for p, v in hits[:80]],
                }
                result["snapshots"].append(snapshot)
                print(f'\nSaved snapshot: {snapshot["device_class"]}, key={key}, UTC={stamp}')
                if hits:
                    print(f'Potential {hint_label}-related fields (NOT validated as consumption):')
                    for path, value in hits[:60]:
                        print(' ', path, '=', repr(value)[:100])
                else:
                    print(f'No matching {hint_label}-related fields in this snapshot.')
                if args.export_json:
                    output = Path(args.export_json)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({'captured_at_utc': stamp,
                                                  'device_key': str(key),
                                                  'device_class': type(entity).__name__,
                                                  'data': payload}, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
                    snapshot["export_json"] = str(output)
                    print('JSON snapshot:', output)
            db.commit()
            result["ok"] = True
            result["db"] = str(args.db)
            write_result(result, args.result_json)
            print('\nSQLite saved at:', args.db)
            print('Note: snapshots are not historical cloud drink events; repeated runs build your own timeline.')
            return 0
    finally:
        if db is not None:
            db.close()
        if not result["ok"] and not result.get("error"):
            result["error"] = result.get("error") or "collection failed"
        write_result(result, args.result_json)


def main():
    p = argparse.ArgumentParser(description='Read-only PETKIT CN device snapshot collector')
    p.add_argument('--db', default=str(DEFAULT_DB))
    p.add_argument('--device-id', help='SDK entity key for your device (from --list-only)')
    p.add_argument('--export-json', help='Optional local JSON snapshot path; keep private')
    p.add_argument('--result-json', help='Machine-readable result file for callers (API service)')
    p.add_argument('--list-only', action='store_true', help='Only list account devices, do not write anything')
    p.add_argument('--hints', default='water', help='water | feeder | comma-separated keywords')
    p.add_argument('--force-login', action='store_true',
                   help='Ignore cached session and login again (WILL log your phone App out)')
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as e:
        print(f'PETKIT request failed: {type(e).__name__}: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
