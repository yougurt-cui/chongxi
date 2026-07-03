#!/usr/bin/env python3
"""Read-only integrity audit for the cat-food standardization chain."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=True
    )


def _rows(cursor, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    return list(cursor.fetchall())


def _count(cursor, sql: str) -> int:
    cursor.execute(sql)
    return int(cursor.fetchone()["n"])


def audit() -> dict[str, Any]:
    with _connect() as conn, conn.cursor() as cursor:
        counts = {
            "brands": _rows(cursor, "SELECT active, COUNT(*) n FROM catfood_standard_brand GROUP BY active ORDER BY active DESC"),
            "products": _rows(cursor, "SELECT active, COUNT(*) n FROM catfood_standard_product GROUP BY active ORDER BY active DESC"),
            "formulas": _rows(cursor, "SELECT status, is_current, COUNT(*) n FROM catfood_standard_formula GROUP BY status, is_current ORDER BY status, is_current DESC"),
            "formula_items": _count(cursor, "SELECT COUNT(*) n FROM catfood_formula_ingredient_item"),
            "ingredients": _rows(cursor, "SELECT active, COUNT(*) n FROM catfood_standard_ingredient GROUP BY active ORDER BY active DESC"),
        }

        product_issues = {
            "orphan_brand": _rows(cursor, """
                SELECT p.product_id, p.brand_id, p.standard_product_name, p.active
                FROM catfood_standard_product p
                LEFT JOIN catfood_standard_brand b ON b.brand_id=p.brand_id
                WHERE b.brand_id IS NULL ORDER BY p.product_id
            """),
            "under_inactive_brand": _rows(cursor, """
                SELECT p.product_id, p.brand_id, b.standard_brand_name, p.standard_product_name, p.active
                FROM catfood_standard_product p
                JOIN catfood_standard_brand b ON b.brand_id=p.brand_id
                WHERE p.active=1 AND b.active=0 ORDER BY p.product_id
            """),
            "active_brands_without_active_products": _rows(cursor, """
                SELECT b.brand_id, b.standard_brand_name
                FROM catfood_standard_brand b
                LEFT JOIN catfood_standard_product p ON p.brand_id=b.brand_id AND p.active=1
                WHERE b.active=1 AND p.product_id IS NULL ORDER BY b.brand_id
            """),
            "duplicate_names_within_brand": _rows(cursor, """
                SELECT brand_id, standard_product_name, COUNT(*) n,
                       GROUP_CONCAT(product_id ORDER BY product_id) product_ids
                FROM catfood_standard_product
                WHERE active=1
                GROUP BY brand_id, standard_product_name HAVING COUNT(*)>1
                ORDER BY brand_id, standard_product_name
            """),
        }

        formula_issues = {
            "orphan_product": _rows(cursor, """
                SELECT f.formula_id, f.product_id, f.formula_version, f.status, f.is_current
                FROM catfood_standard_formula f
                LEFT JOIN catfood_standard_product p ON p.product_id=f.product_id
                WHERE p.product_id IS NULL ORDER BY f.formula_id
            """),
            "current_active_formula_under_inactive_product": _rows(cursor, """
                SELECT f.formula_id, f.product_id, p.standard_product_name
                FROM catfood_standard_formula f
                JOIN catfood_standard_product p ON p.product_id=f.product_id
                WHERE f.status='active' AND f.is_current=1 AND p.active=0
                ORDER BY f.formula_id
            """),
            "active_products_without_current_formula": _rows(cursor, """
                SELECT p.product_id, p.brand_id, p.standard_product_name
                FROM catfood_standard_product p
                LEFT JOIN catfood_standard_formula f
                  ON f.product_id=p.product_id AND f.status='active' AND f.is_current=1
                WHERE p.active=1 AND f.formula_id IS NULL ORDER BY p.product_id
            """),
            "products_with_multiple_current_formulas": _rows(cursor, """
                SELECT f.product_id, COUNT(*) n, GROUP_CONCAT(f.formula_id ORDER BY f.formula_id) formula_ids
                FROM catfood_standard_formula f
                JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                WHERE f.status='active' AND f.is_current=1
                GROUP BY f.product_id HAVING COUNT(*)>1 ORDER BY f.product_id
            """),
            "duplicate_fingerprints_within_product": _rows(cursor, """
                SELECT product_id, ingredient_fingerprint, COUNT(*) n,
                       GROUP_CONCAT(formula_id ORDER BY formula_id) formula_ids
                FROM catfood_standard_formula
                GROUP BY product_id, ingredient_fingerprint HAVING COUNT(*)>1
                ORDER BY product_id, ingredient_fingerprint
            """),
            "current_formulas_without_items": _rows(cursor, """
                SELECT f.formula_id, f.product_id
                FROM catfood_standard_formula f
                JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                LEFT JOIN catfood_formula_ingredient_item i ON i.formula_id=f.formula_id
                WHERE f.status='active' AND f.is_current=1 AND i.item_id IS NULL
                ORDER BY f.formula_id
            """),
        }

        item_issues = {
            "orphan_formula": _rows(cursor, """
                SELECT i.item_id, i.formula_id, i.position, i.raw_name
                FROM catfood_formula_ingredient_item i
                LEFT JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
                WHERE f.formula_id IS NULL ORDER BY i.item_id
            """),
            "unmatched_current_items": _rows(cursor, """
                SELECT i.item_id, i.formula_id, i.position, i.raw_name
                FROM catfood_formula_ingredient_item i
                JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
                JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                WHERE f.status='active' AND f.is_current=1 AND i.standard_ingredient_id IS NULL
                ORDER BY i.formula_id, i.position
            """),
            "matched_to_missing_or_inactive_ingredient": _rows(cursor, """
                SELECT i.item_id, i.formula_id, i.position, i.raw_name,
                       i.standard_ingredient_id, s.standard_name, s.active ingredient_active
                FROM catfood_formula_ingredient_item i
                JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
                JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                LEFT JOIN catfood_standard_ingredient s
                  ON s.standard_ingredient_id=i.standard_ingredient_id
                WHERE f.status='active' AND f.is_current=1
                  AND i.standard_ingredient_id IS NOT NULL
                  AND (s.standard_ingredient_id IS NULL OR s.active=0)
                ORDER BY i.formula_id, i.position
            """),
            "standard_name_mismatch": _rows(cursor, """
                SELECT i.item_id, i.formula_id, i.standard_ingredient_id,
                       i.standard_name item_standard_name, s.standard_name master_standard_name
                FROM catfood_formula_ingredient_item i
                JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
                JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                JOIN catfood_standard_ingredient s
                  ON s.standard_ingredient_id=i.standard_ingredient_id AND s.active=1
                WHERE f.status='active' AND f.is_current=1
                  AND COALESCE(i.standard_name,'')<>COALESCE(s.standard_name,'')
                ORDER BY i.formula_id, i.position
            """),
            "classification_mismatch_count": _count(cursor, """
                SELECT COUNT(*) n
                FROM catfood_formula_ingredient_item i
                JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
                JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                JOIN catfood_standard_ingredient s
                  ON s.standard_ingredient_id=i.standard_ingredient_id AND s.active=1
                WHERE f.status='active' AND f.is_current=1 AND (
                  COALESCE(i.ingredient_family,'')<>COALESCE(s.ingredient_family,'') OR
                  COALESCE(i.source_type,'')<>COALESCE(s.source_type,'') OR
                  COALESCE(i.primary_nutrition_role,'')<>COALESCE(s.primary_nutrition_role,'')
                )
            """),
            "duplicate_positions": _rows(cursor, """
                SELECT formula_id, position, COUNT(*) n, GROUP_CONCAT(item_id ORDER BY item_id) item_ids
                FROM catfood_formula_ingredient_item
                GROUP BY formula_id, position HAVING COUNT(*)>1 ORDER BY formula_id, position
            """),
        }

        ingredient_usage = {
            "active_ingredients_used_by_current_formulas": _count(cursor, """
                SELECT COUNT(DISTINCT i.standard_ingredient_id) n
                FROM catfood_formula_ingredient_item i
                JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
                JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                JOIN catfood_standard_ingredient s
                  ON s.standard_ingredient_id=i.standard_ingredient_id AND s.active=1
                WHERE f.status='active' AND f.is_current=1
            """),
            "active_ingredients_not_used_by_current_formulas": _rows(cursor, """
                SELECT s.standard_ingredient_id, s.standard_name
                FROM catfood_standard_ingredient s
                LEFT JOIN (
                  SELECT DISTINCT i.standard_ingredient_id
                  FROM catfood_formula_ingredient_item i
                  JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
                  JOIN catfood_standard_product p ON p.product_id=f.product_id AND p.active=1
                  WHERE f.status='active' AND f.is_current=1
                ) u ON u.standard_ingredient_id=s.standard_ingredient_id
                WHERE s.active=1 AND u.standard_ingredient_id IS NULL
                ORDER BY s.standard_ingredient_id
            """),
        }

    return {
        "counts": counts,
        "product_issues": product_issues,
        "formula_issues": formula_issues,
        "item_issues": item_issues,
        "ingredient_usage": ingredient_usage,
    }


def main() -> int:
    result = audit()
    summary = json.loads(json.dumps(result, ensure_ascii=False, default=str))
    for section in ("product_issues", "formula_issues", "item_issues"):
        for key, value in list(summary[section].items()):
            if isinstance(value, list):
                summary[section][key] = {"count": len(value), "samples": value[:20]}
    unused = summary["ingredient_usage"]["active_ingredients_not_used_by_current_formulas"]
    summary["ingredient_usage"]["active_ingredients_not_used_by_current_formulas"] = {
        "count": len(unused), "samples": unused[:20]
    }
    print(json.dumps(summary, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
