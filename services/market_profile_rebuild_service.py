"""Asynchronous full-market profile rebuilds triggered by one formula change."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql

from app_config import get_feature_mysql_config


BASE_DIR = Path(__file__).resolve().parents[1]
TASK_TABLE = "market_profile_rebuild_task"
LOCK_NAME = "chongxi_market_profile_full_rebuild"
STEPS = (
    ("module_ranking", "build_module_market_ranking.py"),
    ("structure_labels", "build_formula_structure_labels.py"),
    ("formula_profile", "build_formula_profile.py"),
)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-profile")


def _connect(*, autocommit: bool = False):
    return pymysql.connect(
        **get_feature_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=autocommit,
    )


def ensure_task_table() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{TASK_TABLE}` (
                    task_id VARCHAR(64) PRIMARY KEY,
                    trigger_formula_id BIGINT NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    current_step VARCHAR(64) NULL,
                    steps_json JSON NULL,
                    error_message TEXT NULL,
                    created_at DATETIME NOT NULL,
                    started_at DATETIME NULL,
                    finished_at DATETIME NULL,
                    updated_at DATETIME NOT NULL,
                    KEY idx_market_profile_task_status (status, created_at),
                    KEY idx_market_profile_task_formula (trigger_formula_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()


def _formula_exists(formula_id: int) -> bool:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM catfood_protein_fat_fiber_score_wide WHERE formula_id=%s LIMIT 1",
                (int(formula_id),),
            )
            return cursor.fetchone() is not None


def _result_counts() -> dict[str, dict[str, int]]:
    tables = (
        "catfood_module_market_ranking",
        "catfood_formula_structure_labels",
        "catfood_formula_profile",
    )
    counts: dict[str, dict[str, int]] = {}
    with _connect() as conn:
        with conn.cursor() as cursor:
            for table in tables:
                cursor.execute(
                    f"SELECT COUNT(*) AS rows_count, "
                    f"COUNT(DISTINCT formula_id) AS formula_count FROM `{table}`"
                )
                row = cursor.fetchone() or {}
                counts[table] = {
                    "rows": int(row.get("rows_count") or 0),
                    "formulas": int(row.get("formula_count") or 0),
                }
    return counts


def _execute_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for step_key, script_name in STEPS:
        proc = subprocess.run(
            [sys.executable, str(BASE_DIR / "scripts" / script_name)],
            cwd=str(BASE_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=1200,
        )
        step = {
            "key": step_key,
            "status": "success" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "log_tail": (proc.stdout or "").splitlines()[-40:],
        }
        steps.append(step)
        if proc.returncode != 0:
            error = RuntimeError(f"步骤 {step_key} 执行失败")
            setattr(error, "steps", steps)
            raise error
    return steps


def rebuild_market_profile_for_orchestrator(payload: dict[str, Any]) -> dict[str, Any]:
    """Synchronously rebuild the full market tables for a pipeline task node."""
    formula_id = int(payload.get("formula_id") or 0)
    if not formula_id:
        raise ValueError("缺少 formula_id")
    if not _formula_exists(formula_id):
        raise ValueError(f"formula_id={formula_id} 尚未进入 score_wide，请先完成前置评分")

    lock_conn = _connect(autocommit=True)
    try:
        with lock_conn.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (LOCK_NAME,))
            if int((cursor.fetchone() or {}).get("acquired") or 0) != 1:
                raise RuntimeError("另一个全量配方画像构建正在运行")
        steps = _execute_steps()
        return {
            "ok": True,
            "formula_id": formula_id,
            "scope": "full_market",
            "steps": steps,
            "counts": _result_counts(),
        }
    finally:
        try:
            with lock_conn.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
        finally:
            lock_conn.close()


def _task_row(task_id: str) -> dict[str, Any] | None:
    ensure_task_table()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{TASK_TABLE}` WHERE task_id=%s", (task_id,))
            row = cursor.fetchone()
    if not row:
        return None
    raw_steps = row.pop("steps_json", None)
    try:
        row["steps"] = json.loads(raw_steps) if raw_steps else []
    except (TypeError, json.JSONDecodeError):
        row["steps"] = []
    for key in ("created_at", "started_at", "finished_at", "updated_at"):
        if isinstance(row.get(key), datetime):
            row[key] = row[key].isoformat(timespec="seconds")
    return row


def get_rebuild_task(task_id: str) -> dict[str, Any] | None:
    return _task_row(str(task_id).strip())


def _update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.now()
    if "steps_json" in fields and not isinstance(fields["steps_json"], str):
        fields["steps_json"] = json.dumps(fields["steps_json"], ensure_ascii=False)
    assignments = ", ".join(f"`{key}`=%s" for key in fields)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE `{TASK_TABLE}` SET {assignments} WHERE task_id=%s",
                (*fields.values(), task_id),
            )
        conn.commit()


def create_rebuild_task(formula_id: int) -> dict[str, Any]:
    formula_id = int(formula_id)
    ensure_task_table()
    if not _formula_exists(formula_id):
        raise ValueError(f"formula_id={formula_id} 尚未进入 score_wide，请先完成前置评分")

    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT task_id FROM `{TASK_TABLE}` "
                "WHERE status IN ('queued','running') ORDER BY created_at DESC LIMIT 1"
            )
            active = cursor.fetchone()
            if active:
                raise RuntimeError(f"已有市场画像重建任务执行中: {active['task_id']}")
            task_id = f"market-profile-{uuid.uuid4().hex[:20]}"
            now = datetime.now()
            steps = [{"key": key, "status": "pending"} for key, _ in STEPS]
            cursor.execute(
                f"""INSERT INTO `{TASK_TABLE}`
                (task_id, trigger_formula_id, status, steps_json, created_at, updated_at)
                VALUES (%s,%s,'queued',%s,%s,%s)""",
                (task_id, formula_id, json.dumps(steps, ensure_ascii=False), now, now),
            )
        conn.commit()

    _executor.submit(_run_rebuild, task_id)
    return _task_row(task_id) or {"task_id": task_id, "status": "queued"}


def _run_rebuild(task_id: str) -> None:
    lock_conn = _connect(autocommit=True)
    steps = [{"key": key, "status": "pending"} for key, _ in STEPS]
    try:
        with lock_conn.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0) AS acquired", (LOCK_NAME,))
            if int((cursor.fetchone() or {}).get("acquired") or 0) != 1:
                raise RuntimeError("另一个市场画像全量重建正在运行")

        _update_task(task_id, status="running", started_at=datetime.now(), steps_json=steps)
        for index, (step_key, script_name) in enumerate(STEPS):
            steps[index]["status"] = "running"
            _update_task(task_id, current_step=step_key, steps_json=steps)
            proc = subprocess.run(
                [sys.executable, str(BASE_DIR / "scripts" / script_name)],
                cwd=str(BASE_DIR),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=1200,
            )
            steps[index].update(
                status="success" if proc.returncode == 0 else "failed",
                returncode=proc.returncode,
                log_tail=(proc.stdout or "").splitlines()[-40:],
            )
            _update_task(task_id, steps_json=steps)
            if proc.returncode != 0:
                raise RuntimeError(f"步骤 {step_key} 执行失败")
        _update_task(
            task_id,
            status="success",
            current_step=None,
            steps_json=steps,
            finished_at=datetime.now(),
            error_message=None,
        )
    except Exception as exc:
        _update_task(
            task_id,
            status="failed",
            steps_json=steps,
            finished_at=datetime.now(),
            error_message=str(exc),
        )
    finally:
        try:
            with lock_conn.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (LOCK_NAME,))
        finally:
            lock_conn.close()
