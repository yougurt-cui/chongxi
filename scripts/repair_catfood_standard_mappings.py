#!/usr/bin/env python3
"""Repair OCR standard mappings against the current brand/product/formula masters."""

from __future__ import annotations

import argparse
import json
import sys
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
    PRODUCT_ALIAS_TABLE,
    PRODUCT_TABLE,
    normalize_name,
    resolve_standard_brand,
    standardize_product,
)


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _mapping_has_exact_product_identity(cursor, mapping: dict[str, Any]) -> bool:
    product_id = mapping.get("product_id")
    raw_name = normalize_name(mapping.get("raw_product_name"))
    if not product_id or not raw_name:
        return False
    cursor.execute(
        f"""
        SELECT standard_product_name, display_name
        FROM `{PRODUCT_TABLE}`
        WHERE product_id = %s AND active = 1
        """,
        (product_id,),
    )
    product = cursor.fetchone()
    if not product:
        return False
    names = {
        normalize_name(product.get("standard_product_name")),
        normalize_name(product.get("display_name")),
    }
    cursor.execute(
        f"""
        SELECT alias_name
        FROM `{PRODUCT_ALIAS_TABLE}`
        WHERE product_id = %s AND active = 1
        """,
        (product_id,),
    )
    names.update(normalize_name(row.get("alias_name")) for row in cursor.fetchall())
    return raw_name in names


def _resolve_mapping_brand(mapping: dict[str, Any]) -> dict[str, Any] | None:
    resolved = resolve_standard_brand(mapping.get("raw_brand_name"))
    if resolved:
        return resolved
    brand_id = mapping.get("brand_id")
    if not brand_id:
        return None
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT brand_id, standard_brand_name
                FROM `{BRAND_TABLE}`
                WHERE brand_id = %s AND active = 1
                """,
                (brand_id,),
            )
            return cursor.fetchone()


def plan_repairs() -> dict[str, Any]:
    plan: dict[str, Any] = {
        "current_formula_products": [],
        "retain_product_clear_formula": [],
        "reset_and_rematch": [],
    }
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT product_id, MAX(formula_version) AS current_version
                FROM `{FORMULA_TABLE}`
                WHERE status = 'active'
                GROUP BY product_id
                HAVING SUM(is_current = 1) <> 1
                """
            )
            plan["current_formula_products"] = [
                {
                    "product_id": int(row["product_id"]),
                    "current_version": int(row["current_version"]),
                }
                for row in cursor.fetchall()
            ]

            cursor.execute(
                f"""
                SELECT m.*
                FROM `{MAPPING_TABLE}` m
                LEFT JOIN `{PRODUCT_TABLE}` p ON p.product_id = m.product_id
                LEFT JOIN `{FORMULA_TABLE}` f ON f.formula_id = m.formula_id
                WHERE (m.product_id IS NOT NULL AND p.product_id IS NULL)
                   OR (m.formula_id IS NOT NULL AND f.formula_id IS NULL)
                ORDER BY m.source_id
                """
            )
            for mapping in cursor.fetchall():
                item = {
                    "source_id": int(mapping["source_id"]),
                    "old_brand_id": mapping.get("brand_id"),
                    "old_product_id": mapping.get("product_id"),
                    "old_formula_id": mapping.get("formula_id"),
                    "raw_brand_name": mapping.get("raw_brand_name"),
                    "raw_product_name": mapping.get("raw_product_name"),
                }
                if _mapping_has_exact_product_identity(cursor, mapping):
                    plan["retain_product_clear_formula"].append(item)
                    continue
                brand = _resolve_mapping_brand(mapping)
                item["new_brand_id"] = brand.get("brand_id") if brand else None
                item["standard_brand_name"] = (
                    brand.get("standard_brand_name") if brand else None
                )
                plan["reset_and_rematch"].append(item)
    return plan


def apply_repairs(plan: dict[str, Any]) -> dict[str, Any]:
    rematch_source_ids = [
        int(item["source_id"])
        for item in plan["reset_and_rematch"]
        if item.get("new_brand_id")
    ]
    with _connect() as conn:
        with conn.cursor() as cursor:
            for item in plan["current_formula_products"]:
                cursor.execute(
                    f"""
                    UPDATE `{FORMULA_TABLE}`
                    SET is_current = (formula_version = %s)
                    WHERE product_id = %s AND status = 'active'
                    """,
                    (item["current_version"], item["product_id"]),
                )
            for item in plan["retain_product_clear_formula"]:
                cursor.execute(
                    f"""
                    UPDATE `{MAPPING_TABLE}`
                    SET formula_id = NULL,
                        formula_status = 'pending',
                        formula_confidence = NULL,
                        overall_status = 'pending',
                        match_evidence_json = JSON_SET(
                          COALESCE(match_evidence_json, JSON_OBJECT()),
                          '$.mapping_repair', 'deleted_formula_requires_review'
                        )
                    WHERE source_id = %s
                    """,
                    (item["source_id"],),
                )
            for item in plan["reset_and_rematch"]:
                brand_id = item.get("new_brand_id")
                cursor.execute(
                    f"""
                    UPDATE `{MAPPING_TABLE}`
                    SET brand_id = %s,
                        brand_status = %s,
                        brand_confidence = %s,
                        product_id = NULL,
                        product_status = 'pending',
                        product_confidence = NULL,
                        formula_id = NULL,
                        formula_status = 'pending',
                        formula_confidence = NULL,
                        overall_status = 'pending',
                        match_evidence_json = JSON_SET(
                          COALESCE(match_evidence_json, JSON_OBJECT()),
                          '$.mapping_repair', 'master_reference_reset'
                        )
                    WHERE source_id = %s
                    """,
                    (
                        brand_id,
                        "matched" if brand_id else "pending",
                        1.0 if brand_id else None,
                        item["source_id"],
                    ),
                )
        conn.commit()

    rematch_results = []
    for source_id in rematch_source_ids:
        result = standardize_product({"source_id": source_id})
        rematch_results.append(
            {
                "source_id": source_id,
                "product_id": result.get("product_id"),
                "product_status": result.get("product_status"),
                "product_match_method": result.get("product_match_method"),
                "review_candidate_id": result.get("review_candidate_id"),
            }
        )
    return {"ok": True, "plan": plan, "rematch_results": rematch_results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply the repair plan")
    args = parser.parse_args()
    plan = plan_repairs()
    result = apply_repairs(plan) if args.apply else {"ok": True, "plan": plan}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
