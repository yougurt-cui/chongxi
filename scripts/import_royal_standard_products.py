#!/usr/bin/env python3
"""Import selected Royal Canin products and formulas from an OCR backup table."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402
from services.catfood_standardization_service import normalize_ingredients  # noqa: E402


SOURCE_TABLE = "catfood_ingredient_ocr_parsed_bak_37cd5046395f"
TARGET_CODES = ("BK34", "K36", "F32", "I27", "FR31", "EP42")
LIFE_STAGES = {
    "K36": "幼猫",
    "F32": "成猫",
    "I27": "成猫",
    "EP42": "成猫",
}
PRODUCT_NAME_RE = re.compile(r"^([A-Za-z]+\d+)(?:（([^）]+)）)?$")


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _load_source_rows(cursor) -> list[dict[str, Any]]:
    cursor.execute(
        f"""
        SELECT id, source_id, product_name, ingredient_composition
        FROM `{SOURCE_TABLE}`
        WHERE brand = %s
        ORDER BY id
        """,
        ("皇家",),
    )
    selected: dict[str, dict[str, Any]] = {}
    for row in cursor.fetchall():
        match = PRODUCT_NAME_RE.match(str(row.get("product_name") or "").strip())
        if not match:
            continue
        code = match.group(1).upper()
        if code not in TARGET_CODES:
            continue
        if code in selected:
            raise ValueError(f"源表中型号重复: {code}")
        row["code"] = code
        row["subtitle"] = match.group(2)
        selected[code] = row

    missing = [code for code in TARGET_CODES if code not in selected]
    if missing:
        raise ValueError(f"源表缺少型号: {', '.join(missing)}")
    return [selected[code] for code in TARGET_CODES]


def _upsert_product(cursor, *, brand_id: int, row: dict[str, Any]) -> int:
    code = row["code"]
    cursor.execute(
        """
        INSERT INTO catfood_standard_product(
          brand_id, standard_product_name, display_name, display_subtitle,
          candidate_type, product_type, life_stage, active, source_candidate_id
        ) VALUES(%s, %s, %s, %s, 'model_code', '干粮', %s, 1, NULL)
        ON DUPLICATE KEY UPDATE
          display_name = VALUES(display_name),
          display_subtitle = VALUES(display_subtitle),
          candidate_type = 'model_code',
          product_type = COALESCE(product_type, VALUES(product_type)),
          life_stage = COALESCE(life_stage, VALUES(life_stage)),
          active = 1,
          product_id = LAST_INSERT_ID(product_id)
        """,
        (brand_id, code, code, row.get("subtitle"), LIFE_STAGES.get(code)),
    )
    return int(cursor.lastrowid)


def _upsert_formula(cursor, *, product_id: int, row: dict[str, Any]) -> tuple[int, int]:
    raw = str(row.get("ingredient_composition") or "").strip()
    normalized, ingredients, fingerprint = normalize_ingredients(raw)
    if not ingredients:
        raise ValueError(f"{row['code']} 的配料为空")

    cursor.execute(
        """
        SELECT formula_version
        FROM catfood_standard_formula
        WHERE product_id = %s AND ingredient_fingerprint = %s
        """,
        (product_id, fingerprint),
    )
    existing = cursor.fetchone()
    if existing:
        formula_version = int(existing["formula_version"])
    else:
        cursor.execute(
            """
            SELECT COALESCE(MAX(formula_version), 0) + 1 AS next_version
            FROM catfood_standard_formula
            WHERE product_id = %s
            """,
            (product_id,),
        )
        formula_version = int(cursor.fetchone()["next_version"])

    cursor.execute(
        """
        INSERT INTO catfood_standard_formula(
          product_id, formula_version, raw_ingredient_example,
          normalized_ingredient_composition, normalized_ingredients_json,
          ingredient_fingerprint, nutrition_json, is_current, status
        ) VALUES(%s, %s, %s, %s, %s, %s, NULL, 1, 'active')
        ON DUPLICATE KEY UPDATE
          raw_ingredient_example = VALUES(raw_ingredient_example),
          normalized_ingredient_composition = VALUES(normalized_ingredient_composition),
          normalized_ingredients_json = VALUES(normalized_ingredients_json),
          is_current = 1,
          status = 'active',
          formula_id = LAST_INSERT_ID(formula_id)
        """,
        (
            product_id,
            formula_version,
            raw,
            normalized,
            json.dumps(ingredients, ensure_ascii=False),
            fingerprint,
        ),
    )
    formula_id = int(cursor.lastrowid)
    cursor.execute(
        """
        UPDATE catfood_standard_formula
        SET is_current = (formula_id = %s)
        WHERE product_id = %s
        """,
        (formula_id, product_id),
    )
    return formula_id, len(ingredients)


def import_products(*, apply: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"applied": apply, "items": []}
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT brand_id
                FROM catfood_standard_brand
                WHERE standard_brand_name = %s AND active = 1
                """,
                ("皇家",),
            )
            brand = cursor.fetchone()
            if not brand:
                raise ValueError("标准品牌“皇家”不存在")
            brand_id = int(brand["brand_id"])
            result["brand_id"] = brand_id

            for row in _load_source_rows(cursor):
                product_id = _upsert_product(cursor, brand_id=brand_id, row=row)
                formula_id, ingredient_count = _upsert_formula(
                    cursor,
                    product_id=product_id,
                    row=row,
                )
                result["items"].append(
                    {
                        "code": row["code"],
                        "source_row_id": int(row["id"]),
                        "source_id": int(row["source_id"]),
                        "product_id": product_id,
                        "formula_id": formula_id,
                        "ingredient_count": ingredient_count,
                    }
                )

        if apply:
            conn.commit()
        else:
            conn.rollback()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write products and formulas; default is a rolled-back dry run",
    )
    args = parser.parse_args()
    print(json.dumps(import_products(apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
