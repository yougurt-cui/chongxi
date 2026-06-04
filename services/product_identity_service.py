"""Product identity correction service for cat-food pipeline rows."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql

from app_config import get_feature_mysql_config, get_mysql_config


BASE_DIR = Path(__file__).resolve().parents[1]
FEATURE_SCRIPT_DIR = BASE_DIR / "vendor" / "feature_score_pipeline" / "scripts"
if str(FEATURE_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(FEATURE_SCRIPT_DIR))

try:
    from brand_normalizer import build_product_key as _build_product_key
except Exception:  # pragma: no cover - defensive fallback for local script path issues
    def _build_product_key(brand: Any, product_name: Any) -> str:
        brand_text = _clean_text(brand)
        product_text = _clean_text(product_name)
        if brand_text and product_text:
            return f"{brand_text}||{product_text}"
        return brand_text or product_text


CSV_IDENTITY_TABLES = (
    {
        "table": "catfood_ingredient_ocr_parsed",
        "assignments": {"brand": "brand", "product_name": "product_name"},
        "where": "source_or_parsed",
    },
    {
        "table": "product_info",
        "assignments": {"product_name": "product_name"},
        "where": "source",
    },
)

FEATURE_IDENTITY_TABLES = (
    {
        "table": "protein_source_aggregate",
        "assignments": {"brand_name": "brand", "product_name": "product_name", "product_key": "product_key"},
        "where": "source_or_key",
    },
    {
        "table": "catfood_fiber_feature_json",
        "assignments": {"brand": "brand", "product_name": "product_name", "product_key": "product_key"},
        "where": "key_only",
    },
    {
        "table": "catfood_fat_material_features",
        "assignments": {"brand": "brand", "product_name": "product_name", "product_key": "product_key"},
        "where": "source_or_key",
    },
    {
        "table": "protein_business_cluster_product_details_scored",
        "assignments": {"brand_name": "brand", "product_name": "product_name", "product_key": "product_key"},
        "where": "source_or_key",
    },
    {
        "table": "catfood_fiber_feature_score",
        "assignments": {"brand": "brand", "product_name": "product_name", "product_key": "product_key"},
        "where": "source_or_key",
    },
    {
        "table": "catfood_fat_material_features_scored",
        "assignments": {"brand": "brand", "product_name": "product_name", "product_key": "product_key"},
        "where": "key_only",
    },
    {
        "table": "catfood_protein_fat_fiber_score_wide",
        "assignments": {"brand": "brand", "product_name": "product_name", "product_key": "product_key"},
        "where": "source_or_key",
    },
    {
        "table": "sku_feature_input",
        "assignments": {"sku_id": "product_key", "sku_name": "product_name", "brand_name": "brand"},
        "where": "sku_key",
    },
    {
        "table": "sku_risk_score_result",
        "assignments": {"sku_id": "product_key", "sku_name": "product_name", "brand_name": "brand"},
        "where": "sku_key",
    },
)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def _connect_csv():
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def _connect_feature():
    cfg = get_feature_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def init_identity_db() -> None:
    with _connect_csv() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS product_identity_corrections (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    correction_id CHAR(32) NOT NULL,
                    source_id BIGINT NULL,
                    parsed_row_id BIGINT NULL,
                    file_sha256 VARCHAR(128) NULL,
                    image_name VARCHAR(512) NULL,
                    old_brand VARCHAR(255) NULL,
                    old_product_name VARCHAR(512) NULL,
                    old_product_key VARCHAR(1024) NULL,
                    new_brand VARCHAR(255) NOT NULL,
                    new_product_name VARCHAR(512) NOT NULL,
                    new_product_key VARCHAR(1024) NOT NULL,
                    reviewer VARCHAR(128) NULL,
                    reason VARCHAR(1024) NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'applied',
                    update_summary JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    applied_at DATETIME NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_correction_id (correction_id),
                    KEY idx_source_id (source_id),
                    KEY idx_parsed_row_id (parsed_row_id),
                    KEY idx_file_sha256 (file_sha256)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()


def lookup_identity(
    *,
    source_id: int | None = None,
    parsed_row_id: int | None = None,
    file_sha256: str | None = None,
    image_name: str | None = None,
) -> dict[str, Any] | None:
    conditions: list[str] = []
    params: list[Any] = []
    if parsed_row_id is not None:
        conditions.append("p.id = %s")
        params.append(parsed_row_id)
    if source_id is not None:
        conditions.append("p.source_id = %s")
        params.append(source_id)
    if file_sha256:
        conditions.append("p.file_sha256 = %s")
        params.append(file_sha256)
    if image_name:
        conditions.append("p.image_name = %s")
        params.append(image_name)
    if not conditions:
        return None

    sql = f"""
        SELECT
            p.id AS parsed_row_id,
            p.source_id,
            p.file_sha256,
            p.image_name,
            p.image_path,
            p.brand,
            p.product_name
        FROM catfood_ingredient_ocr_parsed p
        WHERE {' OR '.join(conditions)}
        ORDER BY p.id DESC
        LIMIT 1
    """
    with _connect_csv() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
    if row:
        row["product_key"] = _build_product_key(row.get("brand"), row.get("product_name"))
    return row


def list_identity_corrections(*, source_id: int | None = None, limit: int = 50) -> dict[str, Any]:
    init_identity_db()
    limit = max(1, min(int(limit or 50), 200))
    where = ""
    params: list[Any] = []
    if source_id is not None:
        where = "WHERE source_id = %s"
        params.append(source_id)
    params.append(limit)
    with _connect_csv() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM product_identity_corrections
                {where}
                ORDER BY id DESC
                LIMIT %s
                """,
                params,
            )
            rows = cursor.fetchall()
    return {"ok": True, "items": rows}


def _suggest_product_name_from_image(image_name: Any) -> str:
    stem = Path(_clean_text(image_name)).stem
    if not stem:
        return ""
    stem = stem.replace("＿", "_")
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) in {8, 10, 12, 14}:
        stem = parts[0]
    return stem.strip()


def list_identity_review_items(*, limit: int = 100) -> dict[str, Any]:
    limit = max(1, min(int(limit or 100), 300))
    with _connect_csv() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id AS parsed_row_id,
                    source_id,
                    image_name,
                    image_path,
                    file_sha256,
                    brand,
                    product_name,
                    ingredient_composition,
                    parse_ts AS created_ts,
                    updated_ts
                FROM catfood_ingredient_ocr_parsed
                WHERE brand IS NULL
                   OR TRIM(brand) = ''
                   OR brand LIKE '未知品牌%%'
                   OR product_name IS NULL
                   OR TRIM(product_name) = ''
                   OR product_name LIKE '未知产品%%'
                ORDER BY updated_ts DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()

    items = []
    for row in rows:
        old_product_key = _build_product_key(row.get("brand"), row.get("product_name"))
        items.append(
            {
                **row,
                "old_product_key": old_product_key,
                "suggested_brand": "",
                "suggested_product_name": _suggest_product_name_from_image(row.get("image_name")),
            }
        )
    return {"ok": True, "items": items, "count": len(items)}


def _columns(cursor, table: str) -> set[str]:
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return {str(row["Field"]) for row in cursor.fetchall()}


def _table_exists(cursor, table: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table,))
    return cursor.fetchone() is not None


def _in_clause(values: list[str]) -> tuple[str, list[str]]:
    placeholders = ", ".join(["%s"] * len(values))
    return f"({placeholders})", values


def _identity_values(brand: str, product_name: str, product_key: str) -> dict[str, str]:
    return {"brand": brand, "product_name": product_name, "product_key": product_key}


def _where_clause(kind: str, cols: set[str], identity: dict[str, Any], old_keys: list[str]) -> tuple[str, list[Any]]:
    source_id = identity.get("source_id")
    parsed_row_id = identity.get("parsed_row_id")
    clauses: list[str] = []
    params: list[Any] = []

    if kind == "source_or_parsed":
        if source_id is not None and "source_id" in cols:
            clauses.append("source_id = %s")
            params.append(source_id)
        if parsed_row_id is not None and "id" in cols:
            clauses.append("id = %s")
            params.append(parsed_row_id)
    elif kind == "source":
        if source_id is not None and "source_id" in cols:
            clauses.append("source_id = %s")
            params.append(source_id)
    elif kind == "source_or_key":
        if source_id is not None and "source_id" in cols:
            clauses.append("source_id = %s")
            params.append(source_id)
        if old_keys and "product_key" in cols:
            clause, clause_params = _in_clause(old_keys)
            clauses.append(f"product_key IN {clause}")
            params.extend(clause_params)
    elif kind == "key_only":
        if old_keys and "product_key" in cols:
            clause, clause_params = _in_clause(old_keys)
            clauses.append(f"product_key IN {clause}")
            params.extend(clause_params)
    elif kind == "sku_key":
        if old_keys and "sku_id" in cols:
            clause, clause_params = _in_clause(old_keys)
            clauses.append(f"sku_id IN {clause}")
            params.extend(clause_params)

    if not clauses:
        return "", []
    return " OR ".join(f"({clause})" for clause in clauses), params


def _collect_feature_product_keys(cursor, identity: dict[str, Any], old_keys: set[str]) -> None:
    source_id = identity.get("source_id")
    if source_id is None:
        return
    for table in (
        "protein_source_aggregate",
        "catfood_fat_material_features",
        "catfood_fiber_feature_score",
        "catfood_protein_fat_fiber_score_wide",
    ):
        if not _table_exists(cursor, table):
            continue
        cols = _columns(cursor, table)
        if "source_id" not in cols or "product_key" not in cols:
            continue
        cursor.execute(f"SELECT DISTINCT product_key FROM `{table}` WHERE source_id = %s", (source_id,))
        for row in cursor.fetchall():
            key = _clean_text(row.get("product_key"))
            if key:
                old_keys.add(key)


def _apply_table_update(
    cursor,
    *,
    table: str,
    assignments: dict[str, str],
    where_kind: str,
    new_values: dict[str, str],
    identity: dict[str, Any],
    old_keys: list[str],
) -> dict[str, Any]:
    if not _table_exists(cursor, table):
        return {"table": table, "status": "skipped", "reason": "table_not_found", "updated_rows": 0}

    cols = _columns(cursor, table)
    set_parts: list[str] = []
    set_params: list[Any] = []
    for column, value_key in assignments.items():
        if column not in cols:
            continue
        set_parts.append(f"`{column}` = %s")
        set_params.append(new_values[value_key])

    if not set_parts:
        return {"table": table, "status": "skipped", "reason": "no_supported_columns", "updated_rows": 0}

    where_sql, where_params = _where_clause(where_kind, cols, identity, old_keys)
    if not where_sql:
        return {"table": table, "status": "skipped", "reason": "no_locator_columns", "updated_rows": 0}

    try:
        cursor.execute(
            f"UPDATE `{table}` SET {', '.join(set_parts)} WHERE {where_sql}",
            set_params + where_params,
        )
        return {"table": table, "status": "updated", "updated_rows": cursor.rowcount}
    except pymysql.err.IntegrityError as exc:
        return {"table": table, "status": "skipped", "reason": "integrity_error", "error": str(exc), "updated_rows": 0}


def apply_identity_correction(payload: dict[str, Any]) -> dict[str, Any]:
    init_identity_db()
    source_id = payload.get("source_id")
    parsed_row_id = payload.get("parsed_row_id")
    source_id = int(source_id) if source_id not in (None, "") else None
    parsed_row_id = int(parsed_row_id) if parsed_row_id not in (None, "") else None
    file_sha256 = _clean_text(payload.get("file_sha256"))
    image_name = _clean_text(payload.get("image_name"))
    new_brand = _clean_text(payload.get("brand") or payload.get("new_brand"))
    new_product_name = _clean_text(payload.get("product_name") or payload.get("new_product_name"))
    dry_run = bool(payload.get("dry_run", False))

    if not new_brand or not new_product_name:
        raise ValueError("brand 和 product_name 不能为空")

    identity = lookup_identity(
        source_id=source_id,
        parsed_row_id=parsed_row_id,
        file_sha256=file_sha256,
        image_name=image_name,
    )
    if not identity:
        raise ValueError("未找到可修正的 OCR 解析记录，请提供 source_id、parsed_row_id、file_sha256 或 image_name")

    new_product_key = _build_product_key(new_brand, new_product_name)
    old_brand = _clean_text(identity.get("brand"))
    old_product_name = _clean_text(identity.get("product_name"))
    old_keys = {
        _clean_text(payload.get("old_product_key")),
        _clean_text(identity.get("product_key")),
    }
    if identity.get("source_id") is not None:
        old_keys.add(f"未知品牌_{identity['source_id']}||未知产品_{identity['source_id']}")
    old_keys.discard("")
    old_keys.discard(new_product_key)

    with _connect_feature() as feature_conn:
        with feature_conn.cursor() as feature_cursor:
            _collect_feature_product_keys(feature_cursor, identity, old_keys)

    old_key_list = sorted(old_keys)
    new_values = _identity_values(new_brand, new_product_name, new_product_key)
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "identity": identity,
        "old_product_keys": old_key_list,
        "new_identity": {
            "brand": new_brand,
            "product_name": new_product_name,
            "product_key": new_product_key,
        },
        "updates": {"csv_labeling": [], "feature": []},
    }
    if dry_run:
        return result

    correction_id = uuid.uuid4().hex
    csv_conn = _connect_csv()
    feature_conn = _connect_feature()
    try:
        with csv_conn.cursor() as csv_cursor:
            csv_cursor.execute(
                """
                INSERT INTO product_identity_corrections (
                    correction_id, source_id, parsed_row_id, file_sha256, image_name,
                    old_brand, old_product_name, old_product_key,
                    new_brand, new_product_name, new_product_key,
                    reviewer, reason, status, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'applying', %s)
                """,
                (
                    correction_id,
                    identity.get("source_id"),
                    identity.get("parsed_row_id"),
                    identity.get("file_sha256"),
                    identity.get("image_name"),
                    old_brand,
                    old_product_name,
                    old_key_list[0] if old_key_list else None,
                    new_brand,
                    new_product_name,
                    new_product_key,
                    _clean_text(payload.get("reviewer")) or None,
                    _clean_text(payload.get("reason")) or None,
                    datetime.now(),
                ),
            )
            for spec in CSV_IDENTITY_TABLES:
                result["updates"]["csv_labeling"].append(
                    _apply_table_update(
                        csv_cursor,
                        table=spec["table"],
                        assignments=spec["assignments"],
                        where_kind=spec["where"],
                        new_values=new_values,
                        identity=identity,
                        old_keys=old_key_list,
                    )
                )

        with feature_conn.cursor() as feature_cursor:
            for spec in FEATURE_IDENTITY_TABLES:
                result["updates"]["feature"].append(
                    _apply_table_update(
                        feature_cursor,
                        table=spec["table"],
                        assignments=spec["assignments"],
                        where_kind=spec["where"],
                        new_values=new_values,
                        identity=identity,
                        old_keys=old_key_list,
                    )
                )

        with csv_conn.cursor() as csv_cursor:
            csv_cursor.execute(
                """
                UPDATE product_identity_corrections
                SET status = 'applied', update_summary = %s, applied_at = %s
                WHERE correction_id = %s
                """,
                (json.dumps(result["updates"], ensure_ascii=False, default=str), datetime.now(), correction_id),
            )
        csv_conn.commit()
        feature_conn.commit()
        result["correction_id"] = correction_id
        return result
    except Exception:
        csv_conn.rollback()
        feature_conn.rollback()
        raise
    finally:
        csv_conn.close()
        feature_conn.close()
