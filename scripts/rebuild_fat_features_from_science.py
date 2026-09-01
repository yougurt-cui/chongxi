#!/usr/bin/env python3
"""Compare or materialize fat feature fields from active science profiles."""

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
from services.fat_science_materialization_service import build_science_features  # noqa: E402


FEATURE_DB = "protein_feature_platform"
TARGET_TABLE = "catfood_fat_material_features"
COMPARISON_TABLE = "catfood_fat_material_science_comparison"
FEATURE_FIELDS = (
    "fat_sources", "fat_source_types", "antioxidant_sources", "antioxidant_types",
    "micronutrient_sources", "micronutrient_types", "omega6_sources", "omega3_sources",
)


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


def _normalized(value: Any) -> list[str]:
    return sorted({part.strip() for part in str(value or "").replace(",", "、").split("、") if part.strip()})


def ensure_comparison_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FEATURE_DB}.{COMPARISON_TABLE} (
          formula_id BIGINT UNSIGNED NOT NULL,
          old_features_json JSON NOT NULL,
          science_features_json JSON NOT NULL,
          changed_fields_json JSON NOT NULL,
          missing_science_profiles_json JSON NOT NULL,
          comparison_status VARCHAR(32) NOT NULL,
          review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
          review_note TEXT NULL,
          source_fingerprint CHAR(64) NOT NULL,
          compared_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (formula_id),
          KEY idx_fat_science_comparison_status (comparison_status),
          KEY idx_fat_science_review_status (review_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def load_data(cursor, formula_ids: list[int] | None = None):
    where = ""
    params: list[Any] = []
    if formula_ids:
        where = " WHERE formula_id IN (" + ",".join(["%s"] * len(formula_ids)) + ")"
        params.extend(formula_ids)
    cursor.execute(
        f"SELECT * FROM {FEATURE_DB}.{TARGET_TABLE}{where} ORDER BY formula_id", params
    )
    old_rows = {int(row["formula_id"]): row for row in cursor.fetchall()}
    if not old_rows:
        return {}, {}, {}
    ids = list(old_rows)
    cursor.execute(
        "SELECT formula_id,position,raw_name,standard_ingredient_id,standard_name,"
        "primary_nutrition_role,is_ignored FROM catfood_formula_ingredient_item WHERE formula_id IN ("
        + ",".join(["%s"] * len(ids)) + ") ORDER BY formula_id,position",
        ids,
    )
    items: dict[int, list[dict[str, Any]]] = {}
    standard_ids: set[str] = set()
    for row in cursor.fetchall():
        items.setdefault(int(row["formula_id"]), []).append(row)
        if row.get("standard_ingredient_id"):
            standard_ids.add(str(row["standard_ingredient_id"]))
    profiles = {}
    if standard_ids:
        profile_ids = sorted(standard_ids)
        cursor.execute(
            "SELECT * FROM catfood_ingredient_science_profile WHERE standard_ingredient_id IN ("
            + ",".join(["%s"] * len(profile_ids)) + ")",
            profile_ids,
        )
        profiles = {str(row["standard_ingredient_id"]): row for row in cursor.fetchall()}
    return old_rows, items, profiles


def build_comparisons(old_rows, items, profiles):
    comparisons = []
    for formula_id, old in old_rows.items():
        science = build_science_features(items.get(formula_id, []), profiles)
        changed = [
            field for field in FEATURE_FIELDS
            if _normalized(old.get(field)) != _normalized(science.get(field))
        ]
        missing = science["missing_science_profiles"]
        status = "missing_science" if missing else "changed" if changed else "matched"
        comparisons.append({
            "formula_id": formula_id,
            "old": {field: old.get(field) for field in FEATURE_FIELDS},
            "science": science,
            "changed": changed,
            "status": status,
        })
    return comparisons


def write_comparisons(cursor, comparisons) -> None:
    ensure_comparison_table(cursor)
    sql = f"""
      INSERT INTO {FEATURE_DB}.{COMPARISON_TABLE}(
        formula_id,old_features_json,science_features_json,changed_fields_json,
        missing_science_profiles_json,comparison_status,source_fingerprint
      ) VALUES(%s,%s,%s,%s,%s,%s,%s)
      ON DUPLICATE KEY UPDATE
        old_features_json=VALUES(old_features_json),
        science_features_json=VALUES(science_features_json),
        changed_fields_json=VALUES(changed_fields_json),
        missing_science_profiles_json=VALUES(missing_science_profiles_json),
        comparison_status=VALUES(comparison_status),
        source_fingerprint=VALUES(source_fingerprint),compared_at=NOW()
    """
    for row in comparisons:
        science = row["science"]
        cursor.execute(sql, (
            row["formula_id"], json.dumps(row["old"], ensure_ascii=False),
            json.dumps({field: science.get(field) for field in FEATURE_FIELDS}, ensure_ascii=False),
            json.dumps(row["changed"], ensure_ascii=False),
            json.dumps(science["missing_science_profiles"], ensure_ascii=False),
            row["status"], science["source_fingerprint"],
        ))


def apply_target(cursor, comparisons) -> None:
    assignments = ",".join(f"{field}=%s" for field in FEATURE_FIELDS)
    sql = (
        f"UPDATE {FEATURE_DB}.{TARGET_TABLE} SET {assignments},profile_status=%s,"
        "profile_version=%s,source_fingerprint=%s,needs_review=%s,review_reason=%s "
        "WHERE formula_id=%s"
    )
    for row in comparisons:
        science = row["science"]
        missing = science["missing_science_profiles"]
        cursor.execute(sql, (
            *(science.get(field) for field in FEATURE_FIELDS),
            science["profile_status"], science["profile_version"], science["source_fingerprint"],
            1 if missing else 0,
            "存在缺少 active 科学属性的相关原料" if missing else None,
            row["formula_id"],
        ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formula-id", action="append", type=int)
    parser.add_argument("--apply-target", action="store_true")
    args = parser.parse_args()
    with _connect() as conn:
        with conn.cursor() as cursor:
            old_rows, items, profiles = load_data(cursor, args.formula_id)
            comparisons = build_comparisons(old_rows, items, profiles)
            write_comparisons(cursor, comparisons)
            if args.apply_target:
                apply_target(cursor, comparisons)
        conn.commit()
    counts: dict[str, int] = {}
    for row in comparisons:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(json.dumps({
        "row_count": len(comparisons), "status_counts": counts,
        "target_updated": bool(args.apply_target),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
