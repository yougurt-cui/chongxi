from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymysql

from app_config import get_cat_food_upload_root, get_mysql_config


BASE_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = BASE_DIR / "var"
UPLOAD_DIR = get_cat_food_upload_root(RUNTIME_DIR / "cat_food_uploads")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect() -> pymysql.connections.Connection:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cfg = get_mysql_config()
    return pymysql.connect(
        host=cfg["host"],
        port=int(cfg.get("port", 3306)),
        user=cfg["user"],
        password=str(cfg.get("password", "")),
        database=cfg["database"],
        charset=str(cfg.get("charset") or "utf8mb4"),
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def init_db() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_task (
                    id VARCHAR(64) PRIMARY KEY,
                    task_type VARCHAR(64) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    progress INT NOT NULL DEFAULT 0,
                    input_hash VARCHAR(128),
                    result_json LONGTEXT,
                    error_message TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    finished_at DATETIME NULL,
                    KEY idx_analysis_task_type_status (task_type, status),
                    KEY idx_analysis_task_updated_at (updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_profile (
                    id VARCHAR(64) PRIMARY KEY,
                    task_id VARCHAR(64) NOT NULL,
                    cat_profile_json LONGTEXT NOT NULL,
                    current_food VARCHAR(512),
                    target_food VARCHAR(512),
                    notes TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    KEY idx_analysis_profile_task_id (task_id),
                    CONSTRAINT fk_analysis_profile_task
                        FOREIGN KEY (task_id) REFERENCES analysis_task(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS uploaded_image (
                    id VARCHAR(64) PRIMARY KEY,
                    task_id VARCHAR(64) NOT NULL,
                    product_name VARCHAR(512),
                    original_filename VARCHAR(512) NOT NULL,
                    storage_path VARCHAR(1024) NOT NULL,
                    content_type VARCHAR(128),
                    file_size BIGINT NOT NULL DEFAULT 0,
                    sha256 VARCHAR(128) NOT NULL,
                    parse_status VARCHAR(32) NOT NULL,
                    parse_result_json LONGTEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    KEY idx_uploaded_image_task_id (task_id),
                    KEY idx_uploaded_image_sha256 (sha256),
                    CONSTRAINT fk_uploaded_image_task
                        FOREIGN KEY (task_id) REFERENCES analysis_task(id)
                        ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_result_cache (
                    id VARCHAR(64) PRIMARY KEY,
                    cache_key VARCHAR(191) NOT NULL UNIQUE,
                    task_type VARCHAR(64) NOT NULL,
                    model VARCHAR(128) NOT NULL,
                    prompt_version VARCHAR(128) NOT NULL,
                    input_hash VARCHAR(128) NOT NULL,
                    result_text LONGTEXT NOT NULL,
                    result_json LONGTEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    KEY idx_llm_result_cache_input_hash (input_hash),
                    KEY idx_llm_result_cache_task_type (task_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _task_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "progress": row["progress"],
        "input_hash": row["input_hash"],
        "result": _json_loads(row["result_json"], None),
        "error_message": row["error_message"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
    }


def create_task(task_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_db()
    task_id = uuid.uuid4().hex
    profile_id = uuid.uuid4().hex
    now = utc_now()
    cat_profile = payload.get("cat_profile") or {}
    current_food = str(payload.get("current_food") or "").strip() or None
    target_food = str(payload.get("target_food") or "").strip() or None
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO analysis_task
                    (id, task_type, status, progress, input_hash, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (task_id, task_type, "pending", 0, stable_hash(payload), now, now),
            )
            cursor.execute(
                """
                INSERT INTO analysis_profile
                    (id, task_id, cat_profile_json, current_food, target_food, notes, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    task_id,
                    json.dumps(cat_profile, ensure_ascii=False),
                    current_food,
                    target_food,
                    str(payload.get("notes") or "").strip() or None,
                    now,
                    now,
                ),
            )
        conn.commit()
    return get_task(task_id) or {"id": task_id}


def update_profile(task_id: str, payload: dict[str, Any]) -> None:
    init_db()
    now = utc_now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE analysis_profile
                SET cat_profile_json = %s, current_food = %s, target_food = %s, updated_at = %s
                WHERE task_id = %s
                """,
                (
                    json.dumps(payload.get("cat_profile") or {}, ensure_ascii=False),
                    str(payload.get("current_food") or "").strip() or None,
                    str(payload.get("target_food") or "").strip() or None,
                    now,
                    task_id,
                ),
            )
            cursor.execute(
                "UPDATE analysis_task SET input_hash = %s, updated_at = %s WHERE id = %s",
                (stable_hash(payload), now, task_id),
            )
        conn.commit()


def set_task_state(
    task_id: str,
    *,
    status: str,
    progress: int,
    result: Any | None = None,
    error_message: str | None = None,
) -> None:
    init_db()
    now = utc_now()
    finished_at = now if status in {"success", "failed"} else None
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE analysis_task
                SET status = %s,
                    progress = %s,
                    result_json = COALESCE(%s, result_json),
                    error_message = %s,
                    updated_at = %s,
                    finished_at = COALESCE(%s, finished_at)
                WHERE id = %s
                """,
                (
                    status,
                    max(0, min(100, int(progress))),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_message,
                    now,
                    finished_at,
                    task_id,
                ),
            )
        conn.commit()


def add_uploaded_image(
    task_id: str,
    *,
    product_name: str,
    original_filename: str,
    storage_path: Path,
    content_type: str,
    file_size: int,
) -> dict[str, Any]:
    init_db()
    image_id = uuid.uuid4().hex
    now = utc_now()
    digest = file_sha256(storage_path)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO uploaded_image
                    (id, task_id, product_name, original_filename, storage_path, content_type,
                     file_size, sha256, parse_status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    image_id,
                    task_id,
                    product_name,
                    original_filename,
                    str(storage_path),
                    content_type,
                    file_size,
                    digest,
                    "pending",
                    now,
                    now,
                ),
            )
        conn.commit()
    return get_image(image_id) or {"id": image_id}


def set_image_parse_result(image_id: str, *, status: str, result: Any | None = None) -> None:
    init_db()
    now = utc_now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE uploaded_image
                SET parse_status = %s,
                    parse_result_json = COALESCE(%s, parse_result_json),
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    now,
                    image_id,
                ),
            )
        conn.commit()


def get_image(image_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM uploaded_image WHERE id = %s", (image_id,))
            row = cursor.fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "product_name": row["product_name"],
        "original_filename": row["original_filename"],
        "storage_path": row["storage_path"],
        "content_type": row["content_type"],
        "file_size": row["file_size"],
        "sha256": row["sha256"],
        "parse_status": row["parse_status"],
        "parse_result": _json_loads(row["parse_result_json"], None),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def get_task(task_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM analysis_task WHERE id = %s", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute("SELECT * FROM analysis_profile WHERE task_id = %s", (task_id,))
            profile = cursor.fetchone()
            cursor.execute(
                "SELECT * FROM uploaded_image WHERE task_id = %s ORDER BY created_at ASC",
                (task_id,),
            )
            images = cursor.fetchall()
    task = _task_from_row(row)
    task["profile"] = (
        {
            "cat_profile": _json_loads(profile["cat_profile_json"], {}),
            "current_food": profile["current_food"],
            "target_food": profile["target_food"],
            "notes": profile["notes"],
        }
        if profile
        else None
    )
    task["images"] = [
        {
            "id": image["id"],
            "product_name": image["product_name"],
            "original_filename": image["original_filename"],
            "content_type": image["content_type"],
            "file_size": image["file_size"],
            "sha256": image["sha256"],
            "parse_status": image["parse_status"],
            "parse_result": _json_loads(image["parse_result_json"], None),
            "created_at": str(image["created_at"]),
            "updated_at": str(image["updated_at"]),
        }
        for image in images
    ]
    return task


def get_cached_llm_result(cache_key: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM llm_result_cache WHERE cache_key = %s", (cache_key,))
            row = cursor.fetchone()
    if not row:
        return None
    return {
        "cache_key": row["cache_key"],
        "task_type": row["task_type"],
        "model": row["model"],
        "prompt_version": row["prompt_version"],
        "input_hash": row["input_hash"],
        "result_text": row["result_text"],
        "result": _json_loads(row["result_json"], None),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def store_llm_result(
    *,
    cache_key: str,
    task_type: str,
    model: str,
    prompt_version: str,
    input_hash: str,
    result_text: str,
    result: Any | None = None,
) -> None:
    init_db()
    now = utc_now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO llm_result_cache
                    (id, cache_key, task_type, model, prompt_version, input_hash,
                     result_text, result_json, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    result_text = VALUES(result_text),
                    result_json = VALUES(result_json),
                    updated_at = VALUES(updated_at)
                """,
                (
                    uuid.uuid4().hex,
                    cache_key,
                    task_type,
                    model,
                    prompt_version,
                    input_hash,
                    result_text,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    now,
                    now,
                ),
            )
        conn.commit()
