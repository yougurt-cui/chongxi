#!/usr/bin/env python3
"""Compare or materialize formula fiber features from active science profiles."""

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
from services.fiber_science_materialization_service import (  # noqa: E402
    build_science_payload,
    structure_labels,
)

FEATURE_DB = "protein_feature_platform"
TARGET_TABLE = "catfood_fiber_feature_json"
COMPARISON_TABLE = "catfood_fiber_feature_science_comparison"


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _legacy_structure_labels(row: dict[str, Any]) -> dict[str, list[str]]:
    feature = _json(row.get("ingredient_feature_json"), {})
    starch = _json(row.get("starch_ingredients_json"), [])
    normalized_starch = []
    reverse = {
        "豆类碳水来源": "legume",
        "谷物淀粉来源": "grain",
        "薯类淀粉来源": "tuber",
        "高淀粉粉类": "flour",
        "精制淀粉/纯淀粉": "refined_starch",
        "非淀粉可利用碳水": "available_sugar",
    }
    for item in starch:
        normalized = dict(item)
        normalized["category_code"] = reverse.get(str(item.get("category") or ""), "")
        normalized_starch.append(normalized)
    # Legacy gel labels were name-based. Preserve them only for comparison.
    for name, info in (feature.get("ingredient_tag_detail") or {}).items():
        if any(marker in name for marker in ("瓜尔", "果胶", "魔芋", "黄原胶", "卡拉胶", "结冷胶", "胶")):
            functions = list(info.get("fiber_functions") or [])
            if "胶质成形" not in functions:
                functions.append("胶质成形")
            info["fiber_functions"] = functions
    return structure_labels(feature, normalized_starch)


def ensure_comparison_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FEATURE_DB}.{COMPARISON_TABLE} (
          formula_id BIGINT UNSIGNED NOT NULL,
          science_profile_coverage DECIMAL(8,6) NOT NULL DEFAULT 0,
          science_profile_used_count INT NOT NULL DEFAULT 0,
          missing_science_profiles_json JSON NOT NULL,
          old_ingredient_feature_json JSON NULL,
          science_ingredient_feature_json JSON NOT NULL,
          old_starch_ingredients_json JSON NULL,
          science_starch_ingredients_json JSON NOT NULL,
          old_structure_labels_json JSON NOT NULL,
          science_structure_labels_json JSON NOT NULL,
          changed_fields_json JSON NOT NULL,
          comparison_status VARCHAR(32) NOT NULL,
          review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
          review_note TEXT NULL,
          source_fingerprint CHAR(64) NOT NULL,
          compared_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (formula_id),
          KEY idx_fiber_science_comparison_status (comparison_status),
          KEY idx_fiber_science_review_status (review_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def load_rows(cursor, formula_ids: list[int] | None = None) -> tuple[dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    where = ""
    params: list[Any] = []
    if formula_ids:
        where = " WHERE f.formula_id IN (" + ",".join(["%s"] * len(formula_ids)) + ")"
        params.extend(formula_ids)
    cursor.execute(
        f"SELECT f.* FROM {FEATURE_DB}.{TARGET_TABLE} f{where} ORDER BY f.formula_id",
        params,
    )
    legacy = {int(row["formula_id"]): row for row in cursor.fetchall()}
    if not legacy:
        return {}, {}, {}
    ids = list(legacy)
    placeholders = ",".join(["%s"] * len(ids))
    cursor.execute(
        f"""
        SELECT i.formula_id,i.position,i.raw_name,i.standard_ingredient_id,
               i.standard_name,i.primary_nutrition_role,i.is_ignored
        FROM catfood_formula_ingredient_item i
        WHERE i.formula_id IN ({placeholders})
        ORDER BY i.formula_id,i.position
        """,
        ids,
    )
    items: dict[int, list[dict[str, Any]]] = {}
    standard_ids: set[str] = set()
    for row in cursor.fetchall():
        items.setdefault(int(row["formula_id"]), []).append(row)
        if row.get("standard_ingredient_id"):
            standard_ids.add(str(row["standard_ingredient_id"]))
    profiles: dict[str, dict[str, Any]] = {}
    if standard_ids:
        profile_ids = sorted(standard_ids)
        cursor.execute(
            "SELECT * FROM catfood_ingredient_science_profile WHERE standard_ingredient_id IN ("
            + ",".join(["%s"] * len(profile_ids))
            + ")",
            profile_ids,
        )
        profiles = {str(row["standard_ingredient_id"]): row for row in cursor.fetchall()}
    return legacy, items, profiles


def build_comparisons(legacy, items, profiles):
    comparisons = []
    for formula_id, old in legacy.items():
        science = build_science_payload(items.get(formula_id, []), profiles)
        old_labels = _legacy_structure_labels(old)
        science_labels = structure_labels(
            science["ingredient_feature_json"], science["starch_ingredients_json"]
        )
        changed = [key for key in ("starch", "fiber", "gut") if old_labels[key] != science_labels[key]]
        status = (
            "missing_science"
            if science["missing_science_profiles"]
            else "changed"
            if changed
            else "matched"
        )
        comparisons.append(
            {
                "formula_id": formula_id,
                "old": old,
                "science": science,
                "old_labels": old_labels,
                "science_labels": science_labels,
                "changed": changed,
                "status": status,
            }
        )
    return comparisons


def write_comparisons(cursor, comparisons) -> None:
    ensure_comparison_table(cursor)
    sql = f"""
      INSERT INTO {FEATURE_DB}.{COMPARISON_TABLE}(
        formula_id,science_profile_coverage,science_profile_used_count,
        missing_science_profiles_json,old_ingredient_feature_json,
        science_ingredient_feature_json,old_starch_ingredients_json,
        science_starch_ingredients_json,old_structure_labels_json,
        science_structure_labels_json,changed_fields_json,comparison_status,
        source_fingerprint
      ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON DUPLICATE KEY UPDATE
        science_profile_coverage=VALUES(science_profile_coverage),
        science_profile_used_count=VALUES(science_profile_used_count),
        missing_science_profiles_json=VALUES(missing_science_profiles_json),
        old_ingredient_feature_json=VALUES(old_ingredient_feature_json),
        science_ingredient_feature_json=VALUES(science_ingredient_feature_json),
        old_starch_ingredients_json=VALUES(old_starch_ingredients_json),
        science_starch_ingredients_json=VALUES(science_starch_ingredients_json),
        old_structure_labels_json=VALUES(old_structure_labels_json),
        science_structure_labels_json=VALUES(science_structure_labels_json),
        changed_fields_json=VALUES(changed_fields_json),
        comparison_status=VALUES(comparison_status),
        source_fingerprint=VALUES(source_fingerprint),compared_at=NOW()
    """
    for row in comparisons:
        science = row["science"]
        old = row["old"]
        cursor.execute(
            sql,
            (
                row["formula_id"], science["science_profile_coverage"],
                science["science_profile_used_count"],
                json.dumps(science["missing_science_profiles"], ensure_ascii=False),
                old.get("ingredient_feature_json"),
                json.dumps(science["ingredient_feature_json"], ensure_ascii=False),
                old.get("starch_ingredients_json"),
                json.dumps(science["starch_ingredients_json"], ensure_ascii=False),
                json.dumps(row["old_labels"], ensure_ascii=False),
                json.dumps(row["science_labels"], ensure_ascii=False),
                json.dumps(row["changed"], ensure_ascii=False), row["status"],
                science["science_source_fingerprint"],
            ),
        )


def apply_target(cursor, comparisons) -> None:
    sql = f"""
      UPDATE {FEATURE_DB}.{TARGET_TABLE}
      SET ingredient_feature_json=%s,starch_ingredients_json=%s,
          profile_status=%s,profile_version=%s,source_fingerprint=%s,
          updated_at=CURRENT_TIMESTAMP
      WHERE formula_id=%s
    """
    for row in comparisons:
        science = row["science"]
        cursor.execute(
            sql,
            (
                json.dumps(science["ingredient_feature_json"], ensure_ascii=False),
                json.dumps(science["starch_ingredients_json"], ensure_ascii=False),
                science["profile_status"], science["profile_version"],
                science["science_source_fingerprint"], row["formula_id"],
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formula-id", action="append", type=int)
    parser.add_argument("--apply-target", action="store_true", help="overwrite the materialized target after comparison")
    args = parser.parse_args()
    with _connect() as conn:
        with conn.cursor() as cursor:
            legacy, items, profiles = load_rows(cursor, args.formula_id)
            comparisons = build_comparisons(legacy, items, profiles)
            write_comparisons(cursor, comparisons)
            if args.apply_target:
                apply_target(cursor, comparisons)
        conn.commit()
    counts: dict[str, int] = {}
    for row in comparisons:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(json.dumps({"row_count": len(comparisons), "status_counts": counts, "target_updated": bool(args.apply_target)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

