"""Maintain formula_id links across legacy feature, score, and risk tables."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pymysql

from app_config import get_feature_mysql_config, get_mysql_config
from services.catfood_standardization_service import (
    FORMULA_INPUT_TABLE,
    MAPPING_TABLE,
    rebuild_formula_feature_inputs,
)


FEATURE_TABLES = (
    "protein_source_aggregate",
    "protein_business_cluster_product_details_scored",
    "catfood_fiber_feature_json",
    "catfood_fiber_feature_score",
    "catfood_fat_material_features",
    "catfood_fat_material_features_scored",
    "catfood_protein_fat_fiber_score_wide",
    "sku_feature_input",
    "sku_risk_score_result",
)


def _connect_csv():
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _connect_feature():
    return pymysql.connect(
        **get_feature_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _table_exists(cursor, table: str) -> bool:
    cursor.execute("SHOW TABLES LIKE %s", (table,))
    return bool(cursor.fetchone())


def _columns(cursor, table: str) -> set[str]:
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return {str(row["Field"]) for row in cursor.fetchall()}


def ensure_formula_id_columns() -> dict[str, Any]:
    changed = []
    existing = []
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            for table in FEATURE_TABLES:
                if not _table_exists(cursor, table):
                    continue
                columns = _columns(cursor, table)
                if "formula_id" not in columns:
                    cursor.execute(
                        f"ALTER TABLE `{table}` ADD COLUMN formula_id BIGINT UNSIGNED NULL"
                    )
                    changed.append(table)
                else:
                    existing.append(table)
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM INFORMATION_SCHEMA.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = %s
                      AND INDEX_NAME = 'idx_formula_id'
                    """,
                    (table,),
                )
                if not int(cursor.fetchone()["count"]):
                    cursor.execute(
                        f"ALTER TABLE `{table}` ADD KEY idx_formula_id (formula_id)"
                    )
        conn.commit()
    return {"changed": changed, "existing": existing}


def _load_formula_identity_maps() -> tuple[dict[int, int], dict[str, int]]:
    source_to_formula: dict[int, int] = {}
    key_candidates: dict[str, set[int]] = defaultdict(set)
    with _connect_csv() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT source_id, formula_id
                FROM `{MAPPING_TABLE}`
                WHERE formula_id IS NOT NULL
                """
            )
            for row in cursor.fetchall():
                source_to_formula[int(row["source_id"])] = int(row["formula_id"])
            cursor.execute(
                f"""
                SELECT formula_id, source_id, source_ids_json, brand, product_name
                FROM `{FORMULA_INPUT_TABLE}`
                """
            )
            for row in cursor.fetchall():
                formula_id = int(row["formula_id"])
                if row.get("source_id") is not None:
                    source_to_formula[int(row["source_id"])] = formula_id
                raw_sources = row.get("source_ids_json")
                if isinstance(raw_sources, str):
                    try:
                        raw_sources = json.loads(raw_sources)
                    except (TypeError, ValueError):
                        raw_sources = []
                for source_id in raw_sources or []:
                    source_to_formula[int(source_id)] = formula_id
                brand = str(row.get("brand") or "").strip()
                product_name = str(row.get("product_name") or "").strip()
                if brand and product_name:
                    key_candidates[f"{brand}||{product_name}"].add(formula_id)
    key_to_formula = {
        key: next(iter(formula_ids))
        for key, formula_ids in key_candidates.items()
        if len(formula_ids) == 1
    }
    return source_to_formula, key_to_formula


def backfill_formula_ids() -> dict[str, Any]:
    rebuild_summary = rebuild_formula_feature_inputs(apply=True)
    schema_summary = ensure_formula_id_columns()
    source_to_formula, key_to_formula = _load_formula_identity_maps()
    table_updates: dict[str, int] = {}
    ambiguous_keys: dict[str, list[int]] = {}

    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            # Existing source-keyed rows provide the strongest bridge.
            for table in FEATURE_TABLES:
                if not _table_exists(cursor, table):
                    continue
                columns = _columns(cursor, table)
                updated = 0
                if "source_id" in columns:
                    cursor.execute(
                        f"""
                        SELECT DISTINCT source_id, product_key
                        FROM `{table}`
                        WHERE source_id IS NOT NULL
                        """
                        if "product_key" in columns
                        else f"SELECT DISTINCT source_id FROM `{table}` WHERE source_id IS NOT NULL"
                    )
                    for row in cursor.fetchall():
                        formula_id = source_to_formula.get(int(row["source_id"]))
                        if not formula_id:
                            continue
                        cursor.execute(
                            f"""
                            UPDATE `{table}`
                            SET formula_id = %s
                            WHERE source_id = %s
                              AND (formula_id IS NULL OR formula_id <> %s)
                            """,
                            (formula_id, row["source_id"], formula_id),
                        )
                        updated += int(cursor.rowcount)
                table_updates[table] = updated

            # The final score-wide table is the authoritative legacy bridge
            # from product_key to formula_id. Intermediate feature tables may
            # contain stale display names from earlier identity corrections.
            if _table_exists(cursor, "catfood_protein_fat_fiber_score_wide"):
                cursor.execute(
                    """
                    SELECT DISTINCT product_key, formula_id
                    FROM catfood_protein_fat_fiber_score_wide
                    WHERE product_key IS NOT NULL
                      AND TRIM(product_key) <> ''
                      AND formula_id IS NOT NULL
                    """
                )
                for row in cursor.fetchall():
                    product_key = str(row["product_key"]).strip()
                    formula_id = int(row["formula_id"])
                    current = key_to_formula.get(product_key)
                    if current and current != formula_id:
                        ambiguous_keys.setdefault(product_key, [current])
                        if formula_id not in ambiguous_keys[product_key]:
                            ambiguous_keys[product_key].append(formula_id)
                        key_to_formula.pop(product_key, None)
                    elif product_key not in ambiguous_keys:
                        key_to_formula[product_key] = formula_id

            for table in FEATURE_TABLES:
                if not _table_exists(cursor, table):
                    continue
                columns = _columns(cursor, table)
                identity_column = (
                    "product_key"
                    if "product_key" in columns
                    else "sku_id"
                    if "sku_id" in columns
                    else None
                )
                if not identity_column:
                    continue
                for key, formula_id in key_to_formula.items():
                    try:
                        cursor.execute(
                            f"""
                            UPDATE `{table}`
                            SET formula_id = %s
                            WHERE `{identity_column}` = %s
                              AND (formula_id IS NULL OR formula_id <> %s)
                            """,
                            (formula_id, key, formula_id),
                        )
                        table_updates[table] = table_updates.get(table, 0) + int(
                            cursor.rowcount
                        )
                    except pymysql.err.IntegrityError as exc:
                        table_updates[f"{table}_skipped_conflicts"] = table_updates.get(
                            f"{table}_skipped_conflicts",
                            0,
                        ) + 1
        conn.commit()

    return {
        "ok": True,
        "formula_inputs": rebuild_summary,
        "schema": schema_summary,
        "table_updates": table_updates,
        "source_identity_count": len(source_to_formula),
        "product_key_identity_count": len(key_to_formula),
        "ambiguous_product_keys": ambiguous_keys,
    }
