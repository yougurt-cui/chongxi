"""Local image uploads for mini-program moments."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymysql
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app_config import get_mysql_config


BASE_DIR = Path(__file__).resolve().parents[1]
UPLOAD_DIR = Path(os.getenv("MINIPROGRAM_UPLOAD_ROOT") or BASE_DIR / "var" / "miniprogram_uploads").expanduser()
TABLE_NAME = "miniprogram_moment_image_upload"
MAX_IMAGE_SIZE = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
CONTENT_TYPE_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
EXTENSION_BY_CONTENT_TYPE = {value: key for key, value in CONTENT_TYPE_BY_EXTENSION.items()}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect_app(autocommit: bool = False):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _clean_user_id(value: Any) -> str:
    user_id = _clean(value, 128)
    if not user_id:
        raise ValueError("user_id 不能为空")
    return user_id


def _safe_segment(value: str, fallback: str = "unknown") -> str:
    segment = re.sub(r"[^0-9a-zA-Z_-]+", "_", value or "").strip("._")
    return segment[:64] or fallback


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dedupe_storage_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成唯一上传文件名：{path.name}")


def _normalize_upload_file(image_file: FileStorage) -> tuple[str, str, str]:
    if not image_file or not image_file.filename:
        raise ValueError("请上传图片文件")
    original = secure_filename(image_file.filename) or "moment-image"
    suffix = Path(original).suffix.lower()
    content_type = _clean(image_file.content_type, 128) or "application/octet-stream"
    if not suffix and content_type in EXTENSION_BY_CONTENT_TYPE:
        suffix = EXTENSION_BY_CONTENT_TYPE[content_type]
        original = f"{original}{suffix}"
    if suffix not in ALLOWED_EXTENSIONS:
        suffix = ".jpg"
        original = f"{Path(original).stem or 'moment-image'}{suffix}"
    if content_type not in ALLOWED_CONTENT_TYPES and content_type != "application/octet-stream":
        raise ValueError("仅支持 jpg、png、webp、gif 图片")
    if content_type == "application/octet-stream":
        content_type = CONTENT_TYPE_BY_EXTENSION.get(suffix, "image/jpeg")
    return original, suffix, content_type


def init_miniprogram_moment_image_tables() -> None:
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    original_filename VARCHAR(512) NOT NULL,
                    storage_path VARCHAR(1024) NOT NULL,
                    public_url VARCHAR(1024) NOT NULL,
                    content_type VARCHAR(128) NOT NULL,
                    file_size BIGINT NOT NULL DEFAULT 0,
                    sha256 VARCHAR(128) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_mini_moment_image_user (user_id, status, created_at),
                    KEY idx_mini_moment_image_sha256 (sha256)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": row.get("id"),
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "url": row.get("public_url") or "",
        "original_filename": row.get("original_filename") or "",
        "content_type": row.get("content_type") or "",
        "file_size": int(row.get("file_size") or 0),
        "sha256": row.get("sha256") or "",
        "created_at": str(row.get("created_at") or ""),
    }


def upload_moment_image(image_file: FileStorage, *, user_id: Any) -> dict[str, Any]:
    cleaned_user_id = _clean_user_id(user_id)
    original, suffix, content_type = _normalize_upload_file(image_file)
    init_miniprogram_moment_image_tables()
    file_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    date_dir = now.strftime("%Y/%m/%d")
    storage_dir = UPLOAD_DIR / "moments" / date_dir / _safe_segment(cleaned_user_id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_segment(Path(original).stem, "moment-image")
    storage_path = _dedupe_storage_path(storage_dir / f"{stem}_{file_id[:8]}{suffix}")
    image_file.save(storage_path)
    file_size = storage_path.stat().st_size
    if file_size <= 0:
        storage_path.unlink(missing_ok=True)
        raise ValueError("图片文件为空")
    if file_size > MAX_IMAGE_SIZE:
        storage_path.unlink(missing_ok=True)
        raise ValueError("图片大小不能超过 10MB")
    digest = _file_sha256(storage_path)
    public_url = f"/api/miniprogram/moment-images/{file_id}"
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    with _connect_app() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                    id,user_id,original_filename,storage_path,public_url,content_type,file_size,sha256,status,created_at,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                """,
                (
                    file_id, cleaned_user_id, original, str(storage_path), public_url,
                    content_type, file_size, digest, timestamp, timestamp,
                ),
            )
        conn.commit()
    return {
        "ok": True,
        "item": {
            "file_id": file_id,
            "url": public_url,
            "original_filename": original,
            "content_type": content_type,
            "file_size": file_size,
            "sha256": digest,
        },
    }


def get_moment_image(file_id: Any) -> dict[str, Any]:
    cleaned_file_id = _clean(file_id, 32)
    if not cleaned_file_id:
        raise ValueError("file_id 不能为空")
    init_miniprogram_moment_image_tables()
    with _connect_app(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {TABLE_NAME} WHERE id=%s AND status='active' LIMIT 1",
                (cleaned_file_id,),
            )
            row = cursor.fetchone()
    if not row:
        raise LookupError("图片不存在")
    storage_path = Path(row["storage_path"])
    try:
        storage_path.resolve().relative_to(UPLOAD_DIR.resolve())
    except ValueError:
        raise LookupError("图片不存在")
    if not storage_path.exists() or not storage_path.is_file():
        raise LookupError("图片不存在")
    return {**_serialize(row), "storage_path": storage_path}
