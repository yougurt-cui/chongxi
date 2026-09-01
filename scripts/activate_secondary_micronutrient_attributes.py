#!/usr/bin/env python3
"""Activate reviewed secondary micronutrient attributes without name matching at runtime."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


PROTEIN_SECONDARY_TYPES = {
    "animal_organ": (
        "STD00397", "STD00398", "STD00412", "STD00424", "STD00428", "STD00433",
        "STD00435", "STD00101", "STD00249", "STD00252", "STD00587", "STD00588",
        "STD00264", "STD00790", "STD00086", "STD00056",
    ),
    "animal_tissue": (
        "STD00396", "STD00425", "STD00434", "STD00141", "STD00572", "STD00250",
        "STD00213",
    ),
    "egg": (
        "STD00422", "STD00423", "STD00431", "STD00438", "STD00154", "STD00016",
        "STD00087", "STD00801", "STD00802",
    ),
}
TRACE_ELEMENTS = {"iron", "zinc", "copper", "manganese", "selenium", "iodine"}


def _object(value):
    if isinstance(value, dict):
        return dict(value)
    return json.loads(value or "{}")


def main() -> int:
    conn = pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )
    changed = []
    try:
        with conn.cursor() as cursor:
            for source_type, standard_ids in PROTEIN_SECONDARY_TYPES.items():
                strength = "strong" if source_type == "animal_organ" else "medium"
                for standard_id in standard_ids:
                    cursor.execute(
                        "SELECT * FROM catfood_ingredient_science_profile "
                        "WHERE standard_ingredient_id=%s FOR UPDATE", (standard_id,)
                    )
                    row = cursor.fetchone()
                    if not row or row["nutrition_category"] != "protein" or row["science_status"] != "active":
                        raise RuntimeError(f"active protein profile missing: {standard_id}")
                    domain = _object(row["domain_attributes_json"])
                    functions = _object(row["function_attributes_json"])
                    if domain.get("micronutrient_source_type") == source_type and functions.get("micronutrient_support") == strength:
                        continue
                    domain["micronutrient_source_type"] = source_type
                    functions["micronutrient_support"] = strength
                    cursor.execute(
                        "UPDATE catfood_ingredient_science_profile SET domain_attributes_json=%s,"
                        "function_attributes_json=%s,review_note=CONCAT_WS('；',NULLIF(review_note,''),%s),"
                        "reviewed_by='codex-micronutrient-integration',reviewed_at=NOW(),"
                        "profile_version=profile_version+1 WHERE standard_ingredient_id=%s",
                        (
                            json.dumps(domain, ensure_ascii=False),
                            json.dumps(functions, ensure_ascii=False),
                            "保留蛋白质主营养，激活跨域微量元素支持",
                            standard_id,
                        ),
                    )
                    changed.append(standard_id)

            cursor.execute(
                "SELECT * FROM catfood_ingredient_science_profile "
                "WHERE nutrition_category='mineral' AND science_status='active' FOR UPDATE"
            )
            for row in cursor.fetchall():
                domain = _object(row["domain_attributes_json"])
                functions = _object(row["function_attributes_json"])
                mineral_type = str(domain.get("mineral_type") or "")
                elements = set(domain.get("mineral_elements") or [])
                source_type = (
                    "natural" if mineral_type == "natural"
                    else "fortified" if mineral_type in {"organic_salt", "chelated"} or elements & TRACE_ELEMENTS
                    else "mineral"
                )
                strength = "strong" if source_type == "fortified" else "medium"
                if domain.get("micronutrient_source_type") == source_type and functions.get("micronutrient_support") == strength:
                    continue
                domain["micronutrient_source_type"] = source_type
                functions["micronutrient_support"] = strength
                cursor.execute(
                    "UPDATE catfood_ingredient_science_profile SET domain_attributes_json=%s,"
                    "function_attributes_json=%s,review_note=CONCAT_WS('；',NULLIF(review_note,''),%s),"
                    "reviewed_by='codex-micronutrient-integration',reviewed_at=NOW(),"
                    "profile_version=profile_version+1 WHERE standard_ingredient_id=%s",
                    (
                        json.dumps(domain, ensure_ascii=False),
                        json.dumps(functions, ensure_ascii=False),
                        "激活矿物质微量元素支持",
                        row["standard_ingredient_id"],
                    ),
                )
                changed.append(row["standard_ingredient_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(json.dumps({"updated_count": len(changed), "standard_ingredient_ids": changed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
