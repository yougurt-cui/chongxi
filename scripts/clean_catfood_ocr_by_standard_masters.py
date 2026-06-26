#!/usr/bin/env python3
"""Clean OCR working tables using standard brand/product/formula masters as authority."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402
from services.catfood_standardization_service import (  # noqa: E402
    BRAND_TABLE,
    FORMULA_TABLE,
    MAPPING_TABLE,
    PRODUCT_TABLE,
)


RESULTS_TABLE = "catfood_ingredient_ocr_results"
PARSED_TABLE = "catfood_ingredient_ocr_parsed"


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _safe_suffix(value: str) -> str:
    suffix = re.sub(r"[^0-9A-Za-z_]+", "_", value).strip("_")
    if not suffix:
        raise ValueError("backup suffix is empty")
    return suffix


def _backup_name(source_table: str, suffix: str) -> str:
    prefix = {
        RESULTS_TABLE: "catfood_ocr_results_bak",
        PARSED_TABLE: "catfood_ocr_parsed_bak",
    }[source_table]
    name = f"{prefix}_{suffix}"
    if len(name) > 64:
        name = name[:64].rstrip("_")
    return name


def build_cleanup_plan() -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM `{PARSED_TABLE}` p
                JOIN `{MAPPING_TABLE}` m ON m.source_id = p.source_id
                JOIN `{BRAND_TABLE}` b ON b.brand_id = m.brand_id
                JOIN `{PRODUCT_TABLE}` sp ON sp.product_id = m.product_id
                JOIN `{FORMULA_TABLE}` f ON f.formula_id = m.formula_id
                WHERE m.brand_status = 'matched'
                  AND m.product_status = 'matched'
                  AND m.formula_status = 'matched'
                  AND (
                    COALESCE(p.brand, '') <> b.standard_brand_name
                    OR COALESCE(p.product_name, '') <> sp.standard_product_name
                    OR COALESCE(p.ingredient_composition, '') <> f.normalized_ingredient_composition
                  )
                """
            )
            sync_count = int(cursor.fetchone()["count"])

            cursor.execute(
                f"""
                SELECT
                  p.id,
                  p.source_id,
                  p.brand,
                  p.product_name,
                  p.parse_batch_id
                FROM `{PARSED_TABLE}` p
                LEFT JOIN `{RESULTS_TABLE}` r ON r.id = p.source_id
                LEFT JOIN `{MAPPING_TABLE}` m ON m.source_id = p.source_id
                WHERE r.id IS NULL
                  AND m.source_id IS NULL
                  AND p.source_id < 0
                ORDER BY p.source_id
                """
            )
            removable_parsed_rows = list(cursor.fetchall())

            cursor.execute(
                f"""
                SELECT r.id AS source_id, r.image_name
                FROM `{RESULTS_TABLE}` r
                LEFT JOIN `{PARSED_TABLE}` p ON p.source_id = r.id
                WHERE p.id IS NULL
                ORDER BY r.id
                """
            )
            results_without_parsed = list(cursor.fetchall())

            cursor.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM `{PARSED_TABLE}` p
                JOIN `{MAPPING_TABLE}` m ON m.source_id = p.source_id
                WHERE m.overall_status <> 'matched'
                """
            )
            pending_rows_preserved = int(cursor.fetchone()["count"])

    return {
        "parsed_rows_to_sync": sync_count,
        "parsed_rows_to_remove": removable_parsed_rows,
        "results_without_parsed_preserved": results_without_parsed,
        "pending_parsed_rows_preserved": pending_rows_preserved,
    }


def _backup_table(cursor, source_table: str, backup_table: str) -> None:
    cursor.execute(f"CREATE TABLE `{backup_table}` LIKE `{source_table}`")
    cursor.execute(f"INSERT INTO `{backup_table}` SELECT * FROM `{source_table}`")


def apply_cleanup(plan: dict[str, Any], *, backup_suffix: str) -> dict[str, Any]:
    suffix = _safe_suffix(backup_suffix)
    results_backup = _backup_name(RESULTS_TABLE, suffix)
    parsed_backup = _backup_name(PARSED_TABLE, suffix)
    source_ids_to_remove = [
        int(row["source_id"]) for row in plan["parsed_rows_to_remove"]
    ]
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW TABLES LIKE %s", (results_backup,))
            if cursor.fetchone():
                raise ValueError(f"backup table already exists: {results_backup}")
            cursor.execute("SHOW TABLES LIKE %s", (parsed_backup,))
            if cursor.fetchone():
                raise ValueError(f"backup table already exists: {parsed_backup}")

            _backup_table(cursor, RESULTS_TABLE, results_backup)
            _backup_table(cursor, PARSED_TABLE, parsed_backup)

            cursor.execute(
                f"""
                UPDATE `{PARSED_TABLE}` p
                JOIN `{MAPPING_TABLE}` m ON m.source_id = p.source_id
                JOIN `{BRAND_TABLE}` b ON b.brand_id = m.brand_id
                JOIN `{PRODUCT_TABLE}` sp ON sp.product_id = m.product_id
                JOIN `{FORMULA_TABLE}` f ON f.formula_id = m.formula_id
                SET p.brand = b.standard_brand_name,
                    p.product_name = sp.standard_product_name,
                    p.ingredient_composition = f.normalized_ingredient_composition,
                    p.updated_ts = NOW()
                WHERE m.brand_status = 'matched'
                  AND m.product_status = 'matched'
                  AND m.formula_status = 'matched'
                """
            )
            synced = int(cursor.rowcount)

            removed = 0
            if source_ids_to_remove:
                placeholders = ", ".join(["%s"] * len(source_ids_to_remove))
                cursor.execute(
                    f"""
                    DELETE FROM `{PARSED_TABLE}`
                    WHERE source_id IN ({placeholders})
                    """,
                    source_ids_to_remove,
                )
                removed = int(cursor.rowcount)
        conn.commit()
    return {
        "ok": True,
        "results_backup": results_backup,
        "parsed_backup": parsed_backup,
        "parsed_rows_synced": synced,
        "parsed_rows_removed": removed,
        "results_rows_deleted": 0,
    }


def verify_cleanup() -> dict[str, int]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            queries = {
                "results_rows": f"SELECT COUNT(*) AS count FROM `{RESULTS_TABLE}`",
                "parsed_rows": f"SELECT COUNT(*) AS count FROM `{PARSED_TABLE}`",
                "matched_parsed_mismatches": f"""
                    SELECT COUNT(*) AS count
                    FROM `{PARSED_TABLE}` p
                    JOIN `{MAPPING_TABLE}` m ON m.source_id = p.source_id
                    JOIN `{BRAND_TABLE}` b ON b.brand_id = m.brand_id
                    JOIN `{PRODUCT_TABLE}` sp ON sp.product_id = m.product_id
                    JOIN `{FORMULA_TABLE}` f ON f.formula_id = m.formula_id
                    WHERE m.brand_status = 'matched'
                      AND m.product_status = 'matched'
                      AND m.formula_status = 'matched'
                      AND (
                        COALESCE(p.brand, '') <> b.standard_brand_name
                        OR COALESCE(p.product_name, '') <> sp.standard_product_name
                        OR COALESCE(p.ingredient_composition, '') <> f.normalized_ingredient_composition
                      )
                """,
                "negative_unmapped_parsed": f"""
                    SELECT COUNT(*) AS count
                    FROM `{PARSED_TABLE}` p
                    LEFT JOIN `{MAPPING_TABLE}` m ON m.source_id = p.source_id
                    WHERE p.source_id < 0 AND m.source_id IS NULL
                """,
            }
            result = {}
            for key, sql in queries.items():
                cursor.execute(sql)
                result[key] = int(cursor.fetchone()["count"])
            return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply cleanup after full backups")
    parser.add_argument(
        "--backup-suffix",
        default=datetime.now().strftime("%Y%m%d_%H%M%S"),
        help="suffix used for backup tables",
    )
    args = parser.parse_args()
    plan = build_cleanup_plan()
    result: dict[str, Any] = {"ok": True, "plan": plan}
    if args.apply:
        result["applied"] = apply_cleanup(plan, backup_suffix=args.backup_suffix)
        result["verification"] = verify_cleanup()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
