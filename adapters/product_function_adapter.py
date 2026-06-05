"""Database access for product function positioning."""

from __future__ import annotations

import re
from typing import Any

import pymysql

from app_config import get_feature_mysql_config


WIDE_SCORE_TABLE = "catfood_protein_fat_fiber_score_wide"
SKU_FEATURE_TABLE = "sku_feature_input"
RISK_RESULT_TABLE = "sku_risk_score_result"
BLACK_CHIN_MODEL_LIKE = "BLACK_CHIN%"
SOFT_STOOL_MODEL_LIKE = "SOFT_STOOL%"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def _build_product_key(brand: Any, product_name: Any) -> str:
    brand_text = _clean_text(brand)
    product_text = _clean_text(product_name)
    if brand_text and product_text:
        return f"{brand_text}||{product_text}"
    return brand_text or product_text


def _connect_feature():
    cfg = get_feature_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def _table_columns(cursor: pymysql.cursors.DictCursor, table_name: str) -> set[str]:
    if not re.match(r"^[A-Za-z0-9_]+$", table_name):
        raise ValueError(f"非法表名：{table_name}")
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
        """,
        (table_name,),
    )
    return {str(row["COLUMN_NAME"]) for row in cursor.fetchall() or []}


def _select_existing_columns(
    cursor: pymysql.cursors.DictCursor,
    table_name: str,
    requested_columns: list[str],
) -> list[str]:
    columns = _table_columns(cursor, table_name)
    selected = [col for col in requested_columns if col in columns]
    if not selected:
        raise RuntimeError(f"{table_name} 缺少可查询字段")
    return selected


def _identity_conditions(
    *,
    source_id: Any = None,
    product_key: Any = None,
    brand: Any = None,
    product_name: Any = None,
    product_key_col: str = "product_key",
    product_name_col: str = "product_name",
    brand_col: str = "brand",
) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if _clean_text(source_id):
        conditions.append("source_id = %s")
        params.append(_clean_text(source_id))
    lookup_key = _clean_text(product_key) or _build_product_key(brand, product_name)
    if lookup_key:
        conditions.append(f"{product_key_col} = %s")
        params.append(lookup_key)
    if _clean_text(product_name):
        conditions.append(f"{product_name_col} = %s")
        params.append(_clean_text(product_name))
    if _clean_text(brand) and _clean_text(product_name):
        conditions.append(f"({brand_col} = %s AND {product_name_col} = %s)")
        params.extend([_clean_text(brand), _clean_text(product_name)])
    return conditions, params


def fetch_product_function_source(
    *,
    source_id: Any = None,
    product_key: Any = None,
    brand: Any = None,
    product_name: Any = None,
) -> dict[str, Any] | None:
    """Fetch one product's raw scores and risk levels from the feature database."""
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            wide_row = _fetch_wide_row(
                cursor,
                source_id=source_id,
                product_key=product_key,
                brand=brand,
                product_name=product_name,
            )
            if not wide_row:
                return None

            resolved_key = _clean_text(wide_row.get("product_key")) or _clean_text(product_key)
            resolved_name = _clean_text(wide_row.get("product_name")) or _clean_text(product_name)
            sku_feature = _fetch_sku_feature_row(cursor, product_key=resolved_key, product_name=resolved_name)
            black_chin_risk = _fetch_risk_row(
                cursor,
                product_key=resolved_key,
                product_name=resolved_name,
                model_like=BLACK_CHIN_MODEL_LIKE,
            )
            soft_stool_risk = _fetch_risk_row(
                cursor,
                product_key=resolved_key,
                product_name=resolved_name,
                model_like=SOFT_STOOL_MODEL_LIKE,
            )

    return {
        "score_wide": wide_row,
        "sku_feature": sku_feature or {},
        "black_chin_risk": black_chin_risk or {},
        "soft_stool_risk": soft_stool_risk or {},
    }


def _fetch_wide_row(
    cursor: pymysql.cursors.DictCursor,
    *,
    source_id: Any = None,
    product_key: Any = None,
    brand: Any = None,
    product_name: Any = None,
) -> dict[str, Any] | None:
    requested = [
        "source_id",
        "product_key",
        "brand",
        "product_name",
        "protein_structure_score",
        "protein_quality_score",
        "fat_oily_score",
        "fat_regulation_score",
        "fat_score",
        "omega_imbalance_score",
        "fat_mix_complexity_score",
        "p_form_score",
        "p_bulk_score",
        "p_buffer",
        "p_total_score",
        "q_feed",
        "q_scfa",
        "q_total_score",
        "starch_burden_score",
    ]
    selected = _select_existing_columns(cursor, WIDE_SCORE_TABLE, requested)
    conditions, params = _identity_conditions(
        source_id=source_id,
        product_key=product_key,
        brand=brand,
        product_name=product_name,
        brand_col="brand",
    )
    if not conditions:
        raise ValueError("需要提供 source_id、product_key、product_name 或 brand+product_name")

    cursor.execute(
        f"""
        SELECT {", ".join(f"`{col}`" for col in selected)}
        FROM `{WIDE_SCORE_TABLE}`
        WHERE {" OR ".join(conditions)}
        ORDER BY source_id DESC
        LIMIT 1
        """,
        params,
    )
    return cursor.fetchone()


def _fetch_sku_feature_row(
    cursor: pymysql.cursors.DictCursor,
    *,
    product_key: str,
    product_name: str,
) -> dict[str, Any] | None:
    requested = [
        "sku_id",
        "sku_name",
        "brand_name",
        "feature_version",
        "protein_score",
        "carb_score",
        "fiber_score",
        "fat_score",
        "prebiotic_score",
        "antioxidant_score",
        "p_buffer",
        "q_feed",
        "q_scfa",
        "created_at",
    ]
    selected = _select_existing_columns(cursor, SKU_FEATURE_TABLE, requested)
    conditions: list[str] = []
    params: list[Any] = []
    if product_key:
        conditions.append("sku_id = %s")
        params.append(product_key)
    if product_name:
        conditions.append("sku_name = %s")
        params.append(product_name)
    if not conditions:
        return None
    cursor.execute(
        f"""
        SELECT {", ".join(f"`{col}`" for col in selected)}
        FROM `{SKU_FEATURE_TABLE}`
        WHERE {" OR ".join(conditions)}
        ORDER BY created_at DESC
        LIMIT 1
        """,
        params,
    )
    return cursor.fetchone()


def _fetch_risk_row(
    cursor: pymysql.cursors.DictCursor,
    *,
    product_key: str,
    product_name: str,
    model_like: str,
) -> dict[str, Any] | None:
    requested = [
        "sku_id",
        "sku_name",
        "brand_name",
        "score_model_version",
        "history_percentile",
        "history_risk_level",
        "current_pool_percentile",
        "current_pool_risk_level",
        "final_risk_type",
        "reason_tags",
        "main_reason_tags",
        "support_reason_tags",
        "fat_detail_tags",
        "all_reason_tags",
        "batch_rank",
        "calculated_at",
        "created_at",
    ]
    selected = _select_existing_columns(cursor, RISK_RESULT_TABLE, requested)
    conditions: list[str] = []
    params: list[Any] = []
    if product_key:
        conditions.append("sku_id = %s")
        params.append(product_key)
    if product_name:
        conditions.append("sku_name = %s")
        params.append(product_name)
    if not conditions:
        return None

    order_col = "calculated_at" if "calculated_at" in selected else "created_at" if "created_at" in selected else "id"
    cursor.execute(
        f"""
        SELECT {", ".join(f"`{col}`" for col in selected)}
        FROM `{RISK_RESULT_TABLE}`
        WHERE ({" OR ".join(conditions)})
          AND score_model_version LIKE %s
        ORDER BY `{order_col}` DESC
        LIMIT 1
        """,
        [*params, model_like],
    )
    return cursor.fetchone()
