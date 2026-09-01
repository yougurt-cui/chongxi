#!/usr/bin/env python3
"""Correct reviewed mineral/protein identities and activate their science profiles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402
from services.ingredient_science_profile_service import empty_function_attributes  # noqa: E402


MINERALS = {
    "STD00037": "copper",
    "STD00066": "zinc",
    "STD00040": "manganese",
}

HYDROLYZED_PROTEINS = {
    "STD00246": "鱼类/海洋蛋白类",
    "STD00552": "兔肉类",
    "STD00555": "鸡肉类",
    "STD00584": "牛肉类",
    "STD00591": "其他动物蛋白类",
}


def _functions(key: str) -> str:
    values = empty_function_attributes()
    values[key] = "strong"
    return json.dumps(values, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    standard_ids = tuple((*MINERALS, *HYDROLYZED_PROTEINS))
    changes = []
    conn = pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )
    try:
        with conn.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(standard_ids))
            cursor.execute(
                f"SELECT standard_ingredient_id,standard_name,ingredient_family,source_type,"
                f"animal_source,primary_nutrition_role FROM catfood_standard_ingredient "
                f"WHERE standard_ingredient_id IN ({placeholders}) FOR UPDATE",
                standard_ids,
            )
            existing = {row["standard_ingredient_id"]: row for row in cursor.fetchall()}
            missing = sorted(set(standard_ids) - set(existing))
            if missing:
                raise RuntimeError(f"standard ingredients missing: {', '.join(missing)}")

            for standard_id, element in MINERALS.items():
                target = {
                    "ingredient_family": "矿物质/电解质类",
                    "source_type": "synthetic",
                    "animal_source": None,
                    "primary_nutrition_role": "矿物质补充",
                    "nutrition_category": "mineral",
                    "nutrition_subtype": "other",
                    "domain_attributes_json": json.dumps({
                        "mineral_type": "chelated",
                        "mineral_elements": [element],
                        "micronutrient_source_type": "fortified",
                    }, ensure_ascii=False),
                    "function_attributes_json": _functions("micronutrient_support"),
                }
                changes.append({"standard_ingredient_id": standard_id, "standard_name": existing[standard_id]["standard_name"], **target})

            for standard_id, family in HYDROLYZED_PROTEINS.items():
                target = {
                    "ingredient_family": family,
                    "source_type": "animal",
                    "animal_source": existing[standard_id]["animal_source"],
                    "primary_nutrition_role": "蛋白质供给",
                    "nutrition_category": "protein",
                    "nutrition_subtype": "hydrolyzed",
                    "domain_attributes_json": json.dumps({
                        "protein_form": "hydrolyzed",
                        "source_specificity": "specific",
                        "plant_protein_form": "none",
                        "animal_source_category": "rabbit" if standard_id == "STD00552" else "fish" if standard_id == "STD00246" else "poultry" if standard_id == "STD00555" else "livestock",
                        "micronutrient_source_type": "none",
                    }, ensure_ascii=False),
                    "function_attributes_json": _functions("protein_supply"),
                }
                changes.append({"standard_ingredient_id": standard_id, "standard_name": existing[standard_id]["standard_name"], **target})

            if args.apply:
                for row in changes:
                    cursor.execute(
                        "UPDATE catfood_standard_ingredient SET ingredient_family=%s,source_type=%s,"
                        "animal_source=%s,primary_nutrition_role=%s WHERE standard_ingredient_id=%s",
                        (row["ingredient_family"], row["source_type"], row["animal_source"],
                         row["primary_nutrition_role"], row["standard_ingredient_id"]),
                    )
                    cursor.execute(
                        "UPDATE catfood_ingredient_science_profile SET nutrition_category=%s,"
                        "nutrition_subtype=%s,domain_attributes_json=%s,function_attributes_json=%s,"
                        "science_status='active',evidence_level='high',"
                        "review_note=CONCAT_WS('；',NULLIF(review_note,''),%s),"
                        "reviewed_by='codex-reviewed-mapping',reviewed_at=NOW(),"
                        "profile_version=profile_version+1 WHERE standard_ingredient_id=%s",
                        (row["nutrition_category"], row["nutrition_subtype"],
                         row["domain_attributes_json"], row["function_attributes_json"],
                         "按标准原料主营养角色修正并激活", row["standard_ingredient_id"]),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"science profile missing: {row['standard_ingredient_id']}")
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(json.dumps({"applied": args.apply, "updated_count": len(changes), "items": changes}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
