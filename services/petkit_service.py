"""PETKIT（小佩）设备数据服务：通过子进程调用独立 petkit venv 里的采集 CLI。

为什么走子进程：pypetkitapi 依赖 Python 3.11+ 的 enum.StrEnum，而 Flask 工程环境是
3.10，无法在进程内 import。子进程用 ~/.workbuddy/binaries/python/envs/petkit（3.13）。

数据落点：
    var/petkit/petkit_water.sqlite3   设备快照（device_snapshots 表）
    var/petkit/snapshots/*.json       每次采集的脱敏全量快照
凭据与 session：~/.petkit/env、~/.petkit/session.json（与手动脚本共享，单会话不复登录）。
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VAR_DIR = BASE_DIR / "var" / "petkit"
SNAPSHOT_DIR = VAR_DIR / "snapshots"
DB_PATH = VAR_DIR / "petkit_water.sqlite3"

CLI_PATH = Path(__file__).resolve().parent / "petkit_snapshot_cli.py"
PETKIT_PYTHON = Path(os.getenv(
    "PETKIT_PYTHON",
    sys.executable,
))

CN_TZ = timezone(timedelta(hours=8))
# 同一时刻只允许一个采集在跑：session 缓存文件与 SQLite 都不是并发安全的
_COLLECT_LOCK = threading.Lock()


class PetkitError(RuntimeError):
    """采集失败（含子进程非零退出、超时、凭据缺失等）。"""


def _check_env():
    if not PETKIT_PYTHON.exists():
        raise PetkitError(
            f"找不到 petkit venv 解释器: {PETKIT_PYTHON}，"
            "可用环境变量 PETKIT_PYTHON 指定"
        )
    if not CLI_PATH.exists():
        raise PetkitError(f"找不到采集 CLI: {CLI_PATH}")


def _run_cli(extra_args, timeout=180):
    _check_env()
    result_path = VAR_DIR / f"cli_result_{os.getpid()}_{threading.get_ident()}.json"
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        result_path.unlink()
    cmd = [
        str(PETKIT_PYTHON), str(CLI_PATH),
        "--result-json", str(result_path),
        *extra_args,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PetkitError(f"采集超时（>{timeout}s）: {exc}") from exc
    result = {}
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except ValueError:
            result = {}
        result_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit={proc.returncode}"
        if result.get("error"):
            detail = f"{result['error']} | {detail}"
        raise PetkitError(f"PETKIT 采集失败: {detail}")
    if not result.get("ok"):
        raise PetkitError(f"PETKIT 采集失败: {result.get('error') or '未知错误'}")
    result["stdout_tail"] = (proc.stdout or "").strip().splitlines()[-3:]
    return result


def list_devices():
    """列出账号下所有设备（不写库）。"""
    with _COLLECT_LOCK:
        result = _run_cli(["--list-only"])
    return {"devices": result.get("devices", []), "session_reused": result.get("session_reused", False)}


def collect(device_id=None, hints="water", save_json=True):
    """采集一次快照：落 SQLite，可选导出脱敏 JSON，返回候选字段与摘要。"""
    device_id = str(device_id).strip() if device_id else None
    # The CLI has its own standalone default for manual use.  API/service calls
    # must always point it at the same database recent_snapshots() reads.
    args = ["--db", str(DB_PATH), "--hints", str(hints or "water")]
    if device_id:
        args += ["--device-id", device_id]
    if save_json:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
        key = device_id or "auto"
        args += ["--export-json", str(SNAPSHOT_DIR / f"{key}_{stamp}.json")]
    with _COLLECT_LOCK:
        result = _run_cli(args)
    for snap in result.get("snapshots", []):
        export = snap.get("export_json")
        if export and Path(export).exists():
            try:
                payload = json.loads(Path(export).read_text(encoding="utf-8"))["data"]
                snap["summary"] = summarize(payload)
            except (OSError, ValueError, KeyError):
                snap["summary"] = None
    return result


def summarize(payload):
    """挑出人工最关心的状态字段（机型差异大，缺的字段直接略过）。"""
    def pick(mapping, *keys):
        return {k: mapping.get(k) for k in keys if k in mapping and mapping.get(k) is not None}

    summary, state = {}, payload.get("state") or {}
    nfo = payload.get("device_nfo") or {}
    summary["device"] = {"key": payload.get("id"), "model": nfo.get("modele_name"),
                         "device_type": nfo.get("device_type")}
    summary["common"] = pick(payload, "mode", "update_at", "sync_time")
    if state.get("feed_state"):
        fs = state["feed_state"]
        summary["feeding"] = {
            "eat_count": fs.get("eat_count"),
            "eat_avg_sec": fs.get("eat_avg"),
            "plan_per_hopper": [fs.get("plan_amount_total1"), fs.get("plan_amount_total2")],
            "real_per_hopper": [fs.get("real_amount_total1"), fs.get("real_amount_total2")],
            "food_level": [state.get("food1"), state.get("food2")],
            "desiccant_left_days": state.get("desiccant_left_days"),
        }
    if "filter_percent" in payload or "electricity" in payload:
        summary["fountain"] = pick(payload, "filter_percent", "filter_warning", "lack_warning",
                                   "today_pump_run_time", "today_clean_water", "low_battery")
        electricity = payload.get("electricity") or {}
        if electricity.get("battery_percent") is not None:
            summary["fountain"]["battery_percent"] = electricity["battery_percent"]
    return summary


def recent_snapshots(device_key=None, limit=50):
    """查询本地 SQLite 里已有的快照记录（不含 raw_json）。"""
    import sqlite3

    if not DB_PATH.exists():
        return []
    limit = max(1, min(int(limit or 50), 500))
    query = ("SELECT id, captured_at_utc, device_key, device_class, device_name, payload_sha256 "
             "FROM device_snapshots")
    params = []
    if device_key:
        query += " WHERE device_key = ?"
        params.append(str(device_key))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    db = sqlite3.connect(DB_PATH)
    try:
        rows = db.execute(query, params).fetchall()
    finally:
        db.close()
    keys = ["id", "captured_at_utc", "device_key", "device_class", "device_name", "payload_sha256"]
    return [dict(zip(keys, row)) for row in rows]


# ── 事件级展开（饭盆）──────────────────────────────────────────

def _ts(value):
    if value in (None, "", 0):
        return ""
    try:
        return datetime.fromtimestamp(int(value), CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, TypeError):
        return ""


def _eat_events(payload):
    events = []
    for rec in (payload.get("device_records") or {}).get("eat") or []:
        for item in rec.get("items") or []:
            start, end = item.get("eat_start_time"), item.get("eat_end_time")
            # item.duration 是事件视频片段时长（4~5 秒），不是进食时长；
            # 真实进食时长 = eat_end_time - eat_start_time（与 feed_state.eat_avg 同口径）。
            eat_sec = int(end) - int(start) if start and end else None
            events.append({
                "day": rec.get("day"),
                "event_id": item.get("event_id"),
                "eat_start": _ts(start),
                "eat_end": _ts(end),
                "eat_sec": eat_sec,
                "clip_sec": item.get("duration"),
                "empty_bowl": item.get("empty"),
                "eat_weight": item.get("eat_weight"),
                "preview_count": sum(1 for k in ("preview", "preview1", "preview2") if item.get(k)),
                "media_api": item.get("media_api"),
            })
    return events


def _feed_events(payload):
    events = []
    for rec in (payload.get("device_records") or {}).get("feed") or []:
        for item in rec.get("items") or []:
            events.append({
                "day": rec.get("day"),
                "event_id": item.get("event_id"),
                "amount_hopper1": item.get("amount1"),
                "amount_hopper2": item.get("amount2"),
                "completed_at": _ts(item.get("completed_at")),
                "clip_sec": item.get("duration"),
                "media_api": item.get("media_api"),
            })
    return events


_EVENT_BUILDERS = {"eat": _eat_events, "feed": _feed_events}


def device_events(device_id, kind="eat"):
    """实时采一次并返回事件级记录（eat=进食明细 / feed=出粮明细）。"""
    builder = _EVENT_BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"kind 只支持 {' / '.join(_EVENT_BUILDERS)}，收到: {kind!r}")
    if not device_id:
        raise ValueError("必须传 device_id（可用 GET /api/petkit/devices 查询）")
    result = collect(device_id=device_id, save_json=True)
    events = []
    for snap in result.get("snapshots", []):
        export = snap.get("export_json")
        if export and Path(export).exists():
            try:
                payload = json.loads(Path(export).read_text(encoding="utf-8"))["data"]
            except (OSError, ValueError, KeyError):
                continue
            events = builder(payload)
    return {"device_id": str(device_id), "kind": kind, "count": len(events), "events": events}
