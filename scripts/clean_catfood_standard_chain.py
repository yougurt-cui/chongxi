#!/usr/bin/env python3
"""Back up and clean the approved cat-food standardization chain."""

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


SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
BACKUP_TABLES = [
    "catfood_standard_brand",
    "catfood_standard_product",
    "catfood_standard_product_alias",
    "catfood_standard_product_candidate",
    "catfood_standard_formula",
    "catfood_formula_feature_input",
    "catfood_formula_ingredient_item",
    "catfood_formula_feature_profile",
    "catfood_standard_ingredient",
    "catfood_standard_ingredient_alias",
    "catfood_standard_ingredient_candidate",
    "catfood_ocr_standard_mapping",
    "product_guarantee",
]


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


def _table_exists(cursor, table: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) n FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
        (table,),
    )
    return bool(cursor.fetchone()["n"])


def _backup(cursor, table: str, suffix: str) -> tuple[str, int]:
    if not SAFE_NAME.fullmatch(table):
        raise ValueError(f"unsafe table name: {table}")
    backup = f"cleanbak_{suffix}_{table}"
    cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
    cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
    return backup, int(cursor.rowcount)


def clean(*, apply: bool) -> dict[str, Any]:
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("""
            SELECT p.product_id
            FROM catfood_standard_product p
            LEFT JOIN catfood_standard_formula f ON f.product_id=p.product_id
            WHERE p.active=1 AND f.formula_id IS NULL
              AND EXISTS (
                SELECT 1 FROM catfood_formula_delete_audit a
                WHERE a.batch_id='fd_01c111612918'
                  AND a.source_database=DATABASE()
                  AND a.source_table='catfood_standard_formula'
                  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(a.row_data,'$.product_id')) AS UNSIGNED)=p.product_id
              )
            ORDER BY p.product_id
        """)
        product_ids = [int(row["product_id"]) for row in cursor.fetchall()]
        cursor.execute("SELECT standard_ingredient_id FROM catfood_standard_ingredient WHERE active=0 ORDER BY standard_ingredient_id")
        inactive_ingredient_ids = [row["standard_ingredient_id"] for row in cursor.fetchall()]

        preview = {
            "products_to_delete": product_ids,
            "inactive_ingredients_to_delete": inactive_ingredient_ids,
        }
        if not apply:
            conn.rollback()
            return {"applied": False, **preview}

        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backups = []
        for table in BACKUP_TABLES:
            if _table_exists(cursor, table):
                backup, row_count = _backup(cursor, table, suffix)
                backups.append({"table": table, "backup": backup, "rows": row_count})

        affected: dict[str, int] = {}
        if product_ids:
            placeholders = ",".join(["%s"] * len(product_ids))
            cursor.execute(
                f"""
                UPDATE catfood_ocr_standard_mapping
                SET product_id=NULL, formula_id=NULL,
                    product_status='pending', formula_status='pending', overall_status='pending',
                    product_confidence=NULL, formula_confidence=NULL,
                    review_note=CONCAT_WS('; ', NULLIF(review_note,''), '无效标准产品已清理，需重新标准化'),
                    reviewed_at=NULL
                WHERE product_id IN ({placeholders})
                """,
                product_ids,
            )
            affected["ocr_mappings_reset"] = int(cursor.rowcount)

            cursor.execute(
                f"""
                UPDATE catfood_standard_product_candidate c
                JOIN catfood_standard_product p ON p.source_candidate_id=c.product_id
                SET c.active=0, c.review_status='rejected',
                    c.reject_reason='对应错误配方及标准产品已清理'
                WHERE p.product_id IN ({placeholders})
                """,
                product_ids,
            )
            affected["product_candidates_rejected"] = int(cursor.rowcount)

            cursor.execute(
                f"DELETE FROM catfood_standard_product_alias WHERE product_id IN ({placeholders})",
                product_ids,
            )
            affected["catfood_standard_product_alias_deleted"] = int(cursor.rowcount)
            cursor.execute(
                f"DELETE FROM catfood_standard_product WHERE product_id IN ({placeholders})",
                product_ids,
            )
            affected["products_deleted"] = int(cursor.rowcount)

        if inactive_ingredient_ids:
            placeholders = ",".join(["%s"] * len(inactive_ingredient_ids))
            cursor.execute(
                f"DELETE FROM catfood_standard_ingredient_alias WHERE standard_ingredient_id IN ({placeholders})",
                inactive_ingredient_ids,
            )
            affected["inactive_ingredient_aliases_deleted"] = int(cursor.rowcount)

        # These are derived snapshots. Rebuild from all current formulas after commit.
        for table in (
            "catfood_formula_ingredient_item",
            "catfood_formula_feature_profile",
            "catfood_standard_ingredient_candidate",
        ):
            cursor.execute(f"DELETE FROM `{table}`")
            affected[f"{table}_cleared"] = int(cursor.rowcount)

        if inactive_ingredient_ids:
            placeholders = ",".join(["%s"] * len(inactive_ingredient_ids))
            cursor.execute(
                f"DELETE FROM catfood_standard_ingredient WHERE standard_ingredient_id IN ({placeholders})",
                inactive_ingredient_ids,
            )
            affected["inactive_ingredients_deleted"] = int(cursor.rowcount)

        conn.commit()
        return {
            "applied": True,
            **preview,
            "backups": backups,
            "affected": affected,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit changes; default previews")
    args = parser.parse_args()
    print(json.dumps(clean(apply=bool(args.apply)), ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
