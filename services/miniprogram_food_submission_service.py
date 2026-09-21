"""Private mini-program food submissions backed by the cat-food OCR pipeline."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymysql
from PIL import Image, ImageOps
from werkzeug.datastructures import FileStorage

from app_config import get_feature_mysql_config, get_mysql_config
from services import cat_food_task_service as task_store
from services.orchestrator_service import (
    apply_node_result,
    cancel_task,
    create_task as create_orchestrator_task,
    get_task as get_orchestrator_task,
)


TABLE_NAME = "miniprogram_food_submission"
IMAGE_TABLE = "miniprogram_food_submission_image"
MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_TOTAL_SIZE = 30 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect(autocommit: bool = False):
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit
    )


def _catalog_key_exists(catalog_key: str) -> bool:
    if not catalog_key:
        return False
    with pymysql.connect(
        **get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=True
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM catfood_product_catalog WHERE catalog_key=%s AND status='active' LIMIT 1",
                (catalog_key,),
            )
            return cursor.fetchone() is not None


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def init_food_submission_tables() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    claimed_brand VARCHAR(255) NOT NULL,
                    claimed_product_name VARCHAR(512) NOT NULL,
                    remark VARCHAR(1000) NULL,
                    cat_food_task_id VARCHAR(64) NULL,
                    orchestrator_task_id VARCHAR(64) NULL,
                    source_id BIGINT NULL,
                    parsed_row_id BIGINT NULL,
                    formula_id BIGINT NULL,
                    standard_product_id BIGINT NULL,
                    catalog_key VARCHAR(128) NULL,
                    recognition_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                    publish_status VARCHAR(32) NOT NULL DEFAULT 'unpublished',
                    recognized_brand VARCHAR(255) NULL,
                    recognized_product_name VARCHAR(512) NULL,
                    ingredient_composition LONGTEXT NULL,
                    guarantee_json LONGTEXT NULL,
                    recognition_result_json LONGTEXT NULL,
                    reviewer VARCHAR(128) NULL,
                    review_note VARCHAR(1000) NULL,
                    reject_reason VARCHAR(1000) NULL,
                    reviewed_at DATETIME NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_food_submission_user (user_id,status,created_at),
                    KEY idx_food_submission_review (review_status,created_at),
                    KEY idx_food_submission_task (orchestrator_task_id),
                    KEY idx_food_submission_catalog (catalog_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {IMAGE_TABLE} (
                    id CHAR(32) NOT NULL,
                    submission_id CHAR(32) NOT NULL,
                    user_id VARCHAR(128) NOT NULL,
                    uploaded_image_id VARCHAR(64) NOT NULL,
                    image_type VARCHAR(32) NULL,
                    sort_order INT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_food_submission_image (submission_id,sort_order),
                    KEY idx_food_submission_image_user (user_id,submission_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def _safe_stem(value: Any) -> str:
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", _clean(value, 80))
    return re.sub(r"\s+", "_", text).strip("._ ") or "food"


def _save_upload(image_file: FileStorage, *, product_name: str, index: int) -> Path:
    if not image_file or not image_file.filename:
        raise ValueError("请上传有效图片")
    image_file.stream.seek(0)
    raw = image_file.read(MAX_IMAGE_SIZE + 1)
    image_file.stream.seek(0)
    if not raw:
        raise ValueError("图片文件为空")
    if len(raw) > MAX_IMAGE_SIZE:
        raise ValueError(f"图片 {image_file.filename} 超过 10MB")
    date_dir = task_store.UPLOAD_DIR / datetime.now().strftime("%Y%m%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    path = date_dir / f"{_safe_stem(product_name)}_{uuid.uuid4().hex[:10]}_{index}.jpg"
    try:
        with Image.open(image_file.stream) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            if image.width > 1800:
                image.thumbnail((1800, 10000), Image.Resampling.LANCZOS)
            image.save(path, "JPEG", quality=92)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise ValueError(f"无法读取图片 {image_file.filename}") from exc
    return path


def _build_evidence(paths: list[Path], *, product_name: str) -> Path:
    images = [Image.open(path).convert("RGB") for path in paths]
    try:
        width, gap = max(image.width for image in images), 24
        height = sum(image.height for image in images) + gap * (len(images) - 1)
        canvas = Image.new("RGB", (width, height), "white")
        top = 0
        for image in images:
            canvas.paste(image, ((width - image.width) // 2, top))
            top += image.height + gap
        output = paths[0].parent / f"{_safe_stem(product_name)}_OCR_{uuid.uuid4().hex[:10]}.jpg"
        canvas.save(output, "JPEG", quality=92)
        return output
    finally:
        for image in images:
            image.close()


def _status_label(row: dict[str, Any]) -> str:
    if row.get("status") == "cancelled":
        return "已撤销"
    if row.get("review_status") == "rejected":
        return "未通过"
    if row.get("publish_status") == "published":
        return "已收录"
    if row.get("review_status") == "approved":
        return "审核通过，正在生成产品数据"
    if row.get("recognition_status") == "failed":
        return "识别失败"
    if row.get("recognition_status") in {"pending", "processing"}:
        return "识别中"
    return "等待审核"


def _sync_recognition(row: dict[str, Any]) -> dict[str, Any]:
    task_id = row.get("orchestrator_task_id")
    if not task_id or row.get("status") != "active":
        return row
    task = get_orchestrator_task(str(task_id))
    if not task:
        return row
    output = (task.get("outputs") or {}).get("ocr_formula") or {}
    task_status = str(task.get("task_status") or "")
    recognition_status = (
        "failed" if task_status == "failed" else
        "success" if output.get("source_id") or output.get("ingredient_composition") else
        "processing"
    )
    values = {
        "recognition_status": recognition_status,
        "source_id": output.get("source_id"),
        "parsed_row_id": output.get("parsed_row_id"),
        "recognized_brand": output.get("brand"),
        "recognized_product_name": output.get("product_name"),
        "ingredient_composition": output.get("ingredient_composition"),
        "guarantee_json": json.dumps(output.get("guarantee") or {}, ensure_ascii=False),
        "recognition_result_json": json.dumps(output, ensure_ascii=False, default=str),
    }
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""UPDATE {TABLE_NAME} SET recognition_status=%s,source_id=COALESCE(%s,source_id),
                parsed_row_id=COALESCE(%s,parsed_row_id),recognized_brand=COALESCE(%s,recognized_brand),
                recognized_product_name=COALESCE(%s,recognized_product_name),
                ingredient_composition=COALESCE(%s,ingredient_composition),guarantee_json=%s,
                recognition_result_json=%s,updated_at=%s WHERE id=%s""",
                (*values.values(), _now(), row["id"]),
            )
        conn.commit()
    return {**row, **values}


def _serialize(row: dict[str, Any], images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    item = {
        "id": row.get("id"),
        "claimed_brand": row.get("claimed_brand") or "",
        "claimed_product_name": row.get("claimed_product_name") or "",
        "remark": row.get("remark") or "",
        "recognized_brand": row.get("recognized_brand") or "",
        "recognized_product_name": row.get("recognized_product_name") or "",
        "ingredient_composition": row.get("ingredient_composition") or "",
        "guarantee": _json_loads(row.get("guarantee_json"), {}),
        "recognition_status": row.get("recognition_status"),
        "review_status": row.get("review_status"),
        "publish_status": row.get("publish_status"),
        "catalog_key": row.get("catalog_key") or "",
        "review_note": row.get("review_note") or "",
        "reject_reason": row.get("reject_reason") or "",
        "status": row.get("status"),
        "status_label": _status_label(row),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }
    if images is not None:
        item["images"] = [
            {"id": image["uploaded_image_id"], "image_type": image.get("image_type") or "other",
             "url": f"/api/miniprogram/food-submissions/{row['id']}/images/{image['uploaded_image_id']}"}
            for image in images
        ]
    return item


def create_food_submission(*, user_id: Any, brand_name: Any, product_name: Any,
                           remark: Any, image_files: list[FileStorage]) -> dict[str, Any]:
    user_id = _clean(user_id, 128)
    brand_name, product_name = _clean(brand_name, 255), _clean(product_name, 512)
    if not user_id:
        raise ValueError("user_id 不能为空")
    if not brand_name:
        raise ValueError("请填写品牌名")
    if not product_name:
        raise ValueError("请填写产品名")
    image_files = [item for item in image_files if item and item.filename]
    if not 1 <= len(image_files) <= 3:
        raise ValueError("请上传 1～3 张产品图片")
    sizes = []
    for item in image_files:
        item.stream.seek(0, 2)
        sizes.append(item.stream.tell())
        item.stream.seek(0)
    if sum(sizes) > MAX_TOTAL_SIZE:
        raise ValueError("全部图片总大小不能超过 30MB")

    init_food_submission_tables()
    submission_id, now = uuid.uuid4().hex, _now()
    saved_paths: list[Path] = []
    try:
        cat_task = task_store.create_task("image_parse", {
            "source": "miniprogram_food_submission", "submission_id": submission_id,
            "submitted_by_user_id": user_id,
        })
        uploaded = []
        for index, image_file in enumerate(image_files, 1):
            path = _save_upload(image_file, product_name=product_name, index=index)
            saved_paths.append(path)
            uploaded.append(task_store.add_uploaded_image(
                cat_task["id"], product_name=product_name,
                original_filename=image_file.filename, storage_path=path,
                content_type="image/jpeg", file_size=path.stat().st_size,
            ))
        evidence = _build_evidence(saved_paths, product_name=product_name)
        saved_paths.append(evidence)
        payload = {
            "source": "miniprogram_food_submission", "submission_id": submission_id,
            "submitted_by_user_id": user_id, "cat_food_task_id": cat_task["id"],
            "image_id": uploaded[0]["id"], "image_ids": [item["id"] for item in uploaded],
            "image_count": len(uploaded), "brand_name": brand_name, "product_name": product_name,
            "image_path": str(evidence), "original_filename": "、".join(item.filename for item in image_files),
            "content_type": "image/jpeg", "file_size": evidence.stat().st_size,
            "sha256": task_store.file_sha256(evidence),
        }
        pipeline_task = create_orchestrator_task("catfood_image_analysis", payload)
        apply_node_result(pipeline_task["id"], "upload_check", call_status="success", output=payload)
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""INSERT INTO {TABLE_NAME}
                    (id,user_id,claimed_brand,claimed_product_name,remark,cat_food_task_id,
                     orchestrator_task_id,recognition_status,review_status,publish_status,status,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'processing','pending','unpublished','active',%s,%s)""",
                    (submission_id, user_id, brand_name, product_name, _clean(remark, 1000) or None,
                     cat_task["id"], pipeline_task["id"], now, now),
                )
                for index, image in enumerate(uploaded):
                    cursor.execute(
                        f"INSERT INTO {IMAGE_TABLE} (id,submission_id,user_id,uploaded_image_id,sort_order,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        (uuid.uuid4().hex, submission_id, user_id, image["id"], index, now),
                    )
            conn.commit()
        return get_food_submission(user_id, submission_id)
    except Exception:
        for path in saved_paths:
            path.unlink(missing_ok=True)
        raise


def list_food_submissions(user_id: Any, *, limit: Any = 50) -> dict[str, Any]:
    init_food_submission_tables()
    limit = max(1, min(int(limit or 50), 100))
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {TABLE_NAME} WHERE user_id=%s ORDER BY created_at DESC LIMIT %s", (_clean(user_id, 128), limit))
            rows = list(cursor.fetchall() or [])
    return {"ok": True, "count": len(rows), "items": [_serialize(_sync_recognition(row)) for row in rows]}


def get_food_submission(user_id: Any, submission_id: Any) -> dict[str, Any]:
    init_food_submission_tables()
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {TABLE_NAME} WHERE id=%s AND user_id=%s LIMIT 1", (_clean(submission_id, 32), _clean(user_id, 128)))
            row = cursor.fetchone()
            if not row:
                raise LookupError("食品添加记录不存在")
            cursor.execute(f"SELECT * FROM {IMAGE_TABLE} WHERE submission_id=%s AND user_id=%s ORDER BY sort_order", (row["id"], row["user_id"]))
            images = list(cursor.fetchall() or [])
    return {"ok": True, "item": _serialize(_sync_recognition(row), images)}


def get_submission_image(user_id: Any, submission_id: Any, image_id: Any) -> dict[str, Any]:
    init_food_submission_tables()
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT uploaded_image_id FROM {IMAGE_TABLE} WHERE submission_id=%s AND user_id=%s AND uploaded_image_id=%s LIMIT 1", (_clean(submission_id, 32), _clean(user_id, 128), _clean(image_id, 64)))
            if not cursor.fetchone():
                raise LookupError("食品图片不存在")
    image = task_store.get_image(_clean(image_id, 64))
    if not image:
        raise LookupError("食品图片不存在")
    path = Path(str(image["storage_path"])).resolve()
    try:
        path.relative_to(task_store.UPLOAD_DIR.resolve())
    except ValueError as exc:
        raise LookupError("食品图片不存在") from exc
    if not path.is_file():
        raise LookupError("食品图片不存在")
    return {"storage_path": path, "content_type": image.get("content_type") or "image/jpeg", "sha256": image.get("sha256")}


def cancel_food_submission(user_id: Any, submission_id: Any) -> dict[str, Any]:
    init_food_submission_tables()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {TABLE_NAME} WHERE id=%s AND user_id=%s FOR UPDATE", (_clean(submission_id, 32), _clean(user_id, 128)))
            row = cursor.fetchone()
            if not row:
                raise LookupError("食品添加记录不存在")
            if row["review_status"] == "approved" or row["publish_status"] == "published":
                raise ValueError("已审核或已收录的食品不能撤销")
            cursor.execute(f"UPDATE {TABLE_NAME} SET status='cancelled',updated_at=%s WHERE id=%s", (_now(), row["id"]))
        conn.commit()
    if row.get("orchestrator_task_id"):
        cancel_task(row["orchestrator_task_id"], reason="用户撤销食品提交")
    return {"ok": True, "id": row["id"], "status": "cancelled"}


def review_food_submission(submission_id: Any, payload: dict[str, Any], *, action: str) -> dict[str, Any]:
    action = _clean(action).lower()
    if action not in {"approve", "reject"}:
        raise ValueError("action 仅支持 approve/reject")
    init_food_submission_tables()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {TABLE_NAME} WHERE id=%s AND status='active' FOR UPDATE", (_clean(submission_id, 32),))
            row = cursor.fetchone()
            if not row:
                raise LookupError("食品添加记录不存在")
            if action == "reject":
                reason = _clean(payload.get("reject_reason"), 1000)
                if not reason:
                    raise ValueError("驳回时必须填写原因")
                cursor.execute(f"UPDATE {TABLE_NAME} SET review_status='rejected',publish_status='unpublished',reviewer=%s,review_note=%s,reject_reason=%s,reviewed_at=%s,updated_at=%s WHERE id=%s", (_clean(payload.get("reviewer"),128) or None,_clean(payload.get("review_note"),1000) or None,reason,_now(),_now(),row["id"]))
            else:
                catalog_key = _clean(payload.get("catalog_key"), 128)
                if row.get("recognition_status") != "success":
                    raise ValueError("配料表识别尚未成功，不能审核通过")
                if catalog_key and not _catalog_key_exists(catalog_key):
                    raise ValueError("catalog_key 不存在或尚未发布")
                cursor.execute(f"UPDATE {TABLE_NAME} SET review_status='approved',publish_status=%s,catalog_key=COALESCE(%s,catalog_key),reviewer=%s,review_note=%s,reject_reason=NULL,reviewed_at=%s,updated_at=%s WHERE id=%s", ("published" if catalog_key else "processing",catalog_key or None,_clean(payload.get("reviewer"),128) or None,_clean(payload.get("review_note"),1000) or None,_now(),_now(),row["id"]))
        conn.commit()
    return {"ok": True, "id": row["id"], "review_status": "approved" if action == "approve" else "rejected", "publish_status": "published" if action == "approve" and _clean(payload.get("catalog_key")) else ("processing" if action == "approve" else "unpublished")}


def list_food_submissions_admin(*, review_status: Any = "pending", limit: Any = 100) -> dict[str, Any]:
    init_food_submission_tables()
    status = _clean(review_status, 32)
    limit = max(1, min(int(limit or 100), 200))
    where, params = ["status='active'"], []
    if status:
        where.append("review_status=%s")
        params.append(status)
    params.append(limit)
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {TABLE_NAME} WHERE {' AND '.join(where)} ORDER BY created_at ASC LIMIT %s",
                params,
            )
            rows = list(cursor.fetchall() or [])
    items = []
    for row in rows:
        row = _sync_recognition(row)
        item = _serialize(row)
        item["user_id"] = row.get("user_id")
        item["orchestrator_task_id"] = row.get("orchestrator_task_id")
        item["source_id"] = row.get("source_id")
        item["parsed_row_id"] = row.get("parsed_row_id")
        items.append(item)
    return {"ok": True, "count": len(items), "items": items}
