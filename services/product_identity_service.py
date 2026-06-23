"""Product identity correction service for cat-food pipeline rows."""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql

from app_config import get_feature_mysql_config, get_mysql_config
from services.catfood_standardization_service import (
    _assign_candidate_brand,
    BRAND_TABLE,
    PRODUCT_ALIAS_TABLE,
    PRODUCT_TABLE,
    init_standardization_db,
    normalize_name,
    resolve_standard_brand,
)


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

PRODUCT_CANDIDATE_TABLE = "catfood_standard_product_candidate"
PRODUCT_REVIEW_STATUSES = {"pending", "needs_manual_review", "approved", "rejected"}
PRODUCT_QUALITY_LEVELS = {"strong", "medium", "weak", "invalid"}
PRODUCT_CANDIDATE_TYPES = {
    "model_code",
    "series_name",
    "official_name",
    "flavor_protein",
    "process_type",
    "function_position",
    "life_stage",
    "feeding_instruction",
    "ingredient_text",
    "ocr_noise",
    "unknown",
}


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


def _json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def list_product_candidate_reviews(
    *,
    status: str = "",
    quality: str = "",
    brand: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 100), 300))
    offset = max(0, int(offset or 0))
    filters: list[str] = []
    params: list[Any] = []
    if status:
        statuses = [item.strip() for item in status.split(",") if item.strip()]
        invalid = [item for item in statuses if item not in PRODUCT_REVIEW_STATUSES]
        if invalid:
            raise ValueError(f"unsupported product review status: {', '.join(invalid)}")
        placeholders = ", ".join(["%s"] * len(statuses))
        filters.append(f"review_status IN ({placeholders})")
        params.extend(statuses)
    if quality:
        if quality not in PRODUCT_QUALITY_LEVELS:
            raise ValueError(f"unsupported quality level: {quality}")
        filters.append("quality_level = %s")
        params.append(quality)
    if brand:
        filters.append("standard_brand_name LIKE %s")
        params.append(f"%{brand}%")
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""

    with _connect_csv() as conn:
        with conn.cursor() as cursor:
            if not _table_exists(cursor, PRODUCT_CANDIDATE_TABLE):
                return {"ok": True, "items": [], "total": 0, "status_counts": {}}
            cursor.execute(
                f"SELECT COUNT(*) AS total FROM `{PRODUCT_CANDIDATE_TABLE}` {where_sql}",
                params,
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                f"""
                SELECT *
                FROM `{PRODUCT_CANDIDATE_TABLE}`
                {where_sql}
                ORDER BY
                  CASE review_status
                    WHEN 'needs_manual_review' THEN 1
                    WHEN 'pending' THEN 2
                    WHEN 'approved' THEN 3
                    ELSE 4
                  END,
                  CASE quality_level
                    WHEN 'strong' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'weak' THEN 3
                    ELSE 4
                  END,
                  updated_at DESC,
                  product_id DESC
                LIMIT %s OFFSET %s
                """,
                [*params, limit, offset],
            )
            rows = cursor.fetchall()
            parsed_row_ids = sorted(
                {
                    int(parsed_row_id)
                    for row in rows
                    for parsed_row_id in _json_value(
                        row.get("parsed_row_ids_json"), []
                    )
                    if str(parsed_row_id).lstrip("-").isdigit()
                }
            )
            image_names_by_parsed_row: dict[int, str] = {}
            if parsed_row_ids:
                placeholders = ", ".join(["%s"] * len(parsed_row_ids))
                cursor.execute(
                    f"""
                    SELECT id, image_name
                    FROM catfood_ingredient_ocr_parsed
                    WHERE id IN ({placeholders})
                    """,
                    parsed_row_ids,
                )
                image_names_by_parsed_row = {
                    int(row["id"]): _clean_text(row.get("image_name"))
                    for row in cursor.fetchall()
                    if _clean_text(row.get("image_name"))
                }
            cursor.execute(
                f"""
                SELECT review_status, COUNT(*) AS count
                FROM `{PRODUCT_CANDIDATE_TABLE}`
                GROUP BY review_status
                """
            )
            status_counts = {
                str(row["review_status"]): int(row["count"])
                for row in cursor.fetchall()
            }

    json_columns = (
        "normalized_tags_json",
        "quality_reasons_json",
        "source_ids_json",
        "parsed_row_ids_json",
        "raw_product_names_json",
        "evidence_json",
        "model_raw_result_json",
    )
    items = []
    for row in rows:
        item = dict(row)
        for column in json_columns:
            item[column.removesuffix("_json")] = _json_value(item.pop(column, None), [])
        item["image_names"] = list(
            dict.fromkeys(
                image_names_by_parsed_row[parsed_row_id]
                for parsed_row_id in item.get("parsed_row_ids", [])
                if parsed_row_id in image_names_by_parsed_row
            )
        )
        items.append(item)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "status_counts": status_counts,
    }


def review_product_candidate(
    product_id: int,
    payload: dict[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    action = str(action or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise ValueError(f"unsupported product review action: {action}")
    payload = dict(payload or {})
    reviewed_product_id = int(product_id)
    init_standardization_db()
    with _connect_csv() as conn:
        with conn.cursor() as cursor:
            if not _table_exists(cursor, PRODUCT_CANDIDATE_TABLE):
                raise ValueError("产品候选表不存在，请先运行产品标准化脚本")
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_CANDIDATE_TABLE}` WHERE product_id = %s FOR UPDATE",
                (int(product_id),),
            )
            current = cursor.fetchone()
            if not current:
                raise KeyError(f"product_id 不存在: {product_id}")

            if action == "reject":
                cursor.execute(
                    f"""
                    UPDATE `{PRODUCT_CANDIDATE_TABLE}`
                    SET review_status = 'rejected',
                        active = 0,
                        reject_reason = COALESCE(%s, reject_reason),
                        model_reason = CONCAT_WS(
                          '\n',
                          NULLIF(model_reason, ''),
                          NULLIF(%s, '')
                        ),
                        updated_at = NOW()
                    WHERE product_id = %s
                    """,
                    (
                        _clean_text(payload.get("reject_reason")) or "人工驳回",
                        _clean_text(payload.get("review_note")),
                        int(product_id),
                    ),
                )
            else:
                brand_name = _clean_text(
                    payload.get("standard_brand_name")
                    or current.get("standard_brand_name")
                )
                product_name = _clean_text(
                    payload.get("standard_product_name")
                    or current.get("standard_product_name")
                )
                display_name = _clean_text(payload.get("display_name"))
                subtitle = _clean_text(payload.get("display_subtitle"))
                candidate_type = _clean_text(
                    payload.get("candidate_type") or current.get("candidate_type")
                )
                quality = _clean_text(
                    payload.get("quality_level") or current.get("quality_level")
                )
                if not brand_name or not product_name or not display_name:
                    raise ValueError("标准品牌、标准产品名和 display_name 不能为空")
                if candidate_type not in PRODUCT_CANDIDATE_TYPES:
                    raise ValueError(f"unsupported candidate_type: {candidate_type}")
                if quality not in {"strong", "medium", "weak"}:
                    raise ValueError(f"unsupported quality level: {quality}")
                tags = payload.get("normalized_tags")
                if isinstance(tags, str):
                    tags = [item.strip() for item in re.split(r"[,，]", tags) if item.strip()]
                if tags is None:
                    tags = _json_value(current.get("normalized_tags_json"), [])
                brand_id = payload.get("brand_id") or current.get("brand_id")
                if not brand_id:
                    brand_row = resolve_standard_brand(brand_name)
                    brand_id = brand_row.get("brand_id") if brand_row else None
                    if brand_row:
                        brand_name = brand_row["standard_brand_name"]
                if not brand_id:
                    raise ValueError("请选择已有标准品牌；产品审核不能创建品牌")
                candidate_for_assignment = dict(current)
                candidate_for_assignment["standard_product_name"] = product_name
                merged = _assign_candidate_brand(
                    cursor,
                    candidate_for_assignment,
                    brand_id=int(brand_id),
                    standard_brand_name=brand_name,
                )
                if merged:
                    cursor.execute(
                        f"""
                        SELECT *
                        FROM `{PRODUCT_CANDIDATE_TABLE}`
                        WHERE brand_id = %s AND standard_product_name = %s
                        ORDER BY product_id
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (brand_id, product_name),
                    )
                    current = cursor.fetchone()
                    if not current:
                        raise RuntimeError("重复产品候选合并后未找到保留记录")
                    reviewed_product_id = int(current["product_id"])
                cursor.execute(
                    f"""
                    UPDATE `{PRODUCT_CANDIDATE_TABLE}`
                    SET brand_id = COALESCE(%s, brand_id),
                        standard_brand_name = %s,
                        standard_product_name = %s,
                        display_name = %s,
                        display_subtitle = %s,
                        candidate_type = %s,
                        quality_level = %s,
                        normalized_tags_json = %s,
                        review_status = 'approved',
                        active = 1,
                        reject_reason = NULL,
                        updated_at = NOW()
                    WHERE product_id = %s
                    """,
                    (
                        brand_id,
                        brand_name,
                        product_name,
                        display_name,
                        subtitle or None,
                        candidate_type,
                        quality,
                        json.dumps(tags or [], ensure_ascii=False),
                        reviewed_product_id,
                    ),
                )
                cursor.execute(
                    f"""
                    INSERT INTO `{PRODUCT_TABLE}`(
                      brand_id, standard_product_name, display_name, display_subtitle,
                      candidate_type, product_type, life_stage, active, source_candidate_id
                    ) VALUES(%s, %s, %s, %s, %s, %s, %s, 1, %s)
                    ON DUPLICATE KEY UPDATE
                      display_name = VALUES(display_name),
                      display_subtitle = VALUES(display_subtitle),
                      candidate_type = VALUES(candidate_type),
                      product_type = VALUES(product_type),
                      life_stage = VALUES(life_stage),
                      active = 1,
                      source_candidate_id = VALUES(source_candidate_id),
                      product_id = LAST_INSERT_ID(product_id)
                    """,
                    (
                        brand_id,
                        product_name,
                        display_name,
                        subtitle or None,
                        candidate_type,
                        current.get("product_type"),
                        current.get("life_stage"),
                        reviewed_product_id,
                    ),
                )
                standard_product_id = int(cursor.lastrowid)
                aliases = {
                    product_name,
                    display_name,
                    *(_json_value(current.get("raw_product_names_json"), []) or []),
                }
                for alias in aliases:
                    alias_name = _clean_text(alias)
                    normalized_alias = normalize_name(alias_name)
                    if not normalized_alias:
                        continue
                    cursor.execute(
                        f"""
                        INSERT INTO `{PRODUCT_ALIAS_TABLE}`(
                          product_id, brand_id, alias_name, normalized_alias, source, active
                        ) VALUES(%s, %s, %s, %s, 'candidate_review', 1)
                        ON DUPLICATE KEY UPDATE
                          product_id = VALUES(product_id),
                          alias_name = VALUES(alias_name),
                          active = 1
                        """,
                        (
                            standard_product_id,
                            brand_id,
                            alias_name,
                            normalized_alias,
                        ),
                    )
                normalized_aliases = {
                    normalize_name(alias)
                    for alias in aliases
                    if normalize_name(alias)
                }
                cursor.execute(
                    """
                    SELECT source_id, raw_product_name
                    FROM catfood_ocr_standard_mapping
                    WHERE brand_id = %s
                      AND product_status IN ('pending', 'conflict', 'blocked')
                    """,
                    (brand_id,),
                )
                matching_source_ids = [
                    row["source_id"]
                    for row in cursor.fetchall()
                    if normalize_name(row.get("raw_product_name")) in normalized_aliases
                ]
                if matching_source_ids:
                    placeholders = ", ".join(["%s"] * len(matching_source_ids))
                    cursor.execute(
                        f"""
                        UPDATE catfood_ocr_standard_mapping
                        SET product_id = %s,
                            product_status = 'matched',
                            product_confidence = 1.0,
                            formula_status = 'pending',
                            overall_status = 'pending'
                        WHERE source_id IN ({placeholders})
                        """,
                        [standard_product_id, *matching_source_ids],
                    )
            cursor.execute(
                f"SELECT * FROM `{PRODUCT_CANDIDATE_TABLE}` WHERE product_id = %s",
                (reviewed_product_id,),
            )
            updated = cursor.fetchone()
        conn.commit()
    return {"ok": True, "item": updated}


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
