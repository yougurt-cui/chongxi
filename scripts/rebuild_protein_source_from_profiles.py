#!/usr/bin/env python3
"""Build the protein label source for formulas admitted by the profile gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config, get_mysql_config  # noqa: E402


TARGET_TABLE = "protein_source_aggregate"
DEFAULT_POSITION_WEIGHTS = [(1, 1, 1.2), (2, 3, 1.0), (4, 5, 0.8),
                            (6, 8, 0.6), (9, 12, 0.4), (13, 9999, 0.2)]


def _position_weight(position: Any, rules: list[tuple[int, int, float]], unknown: float) -> float:
    try:
        rank = int(position)
    except (TypeError, ValueError):
        return unknown
    return next((weight for start, end, weight in rules if start <= rank <= end), unknown)


def _json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return default
    return parsed


def _join(values: Any) -> str | None:
    items = _json(values, [])
    if not isinstance(items, list):
        return None
    cleaned = list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))
    return "、".join(cleaned) or None


def _crude_protein(nutrition_json: Any) -> tuple[str | None, Any, str | None]:
    nutrition = _json(nutrition_json, {})
    if not isinstance(nutrition, dict):
        return None, None, None
    candidates = [
        item for item in nutrition.values()
        if isinstance(item, dict) and str(item.get("metric_name") or "").strip() == "粗蛋白"
    ]
    if not candidates:
        return None, None, None
    item = sorted(candidates, key=lambda row: str(row.get("basis") or "") == "干物质", reverse=True)[0]
    return "粗蛋白", item.get("value"), item.get("unit")


def _main_form(forms: list[str]) -> str | None:
    if not forms:
        return None
    leading = forms[:3]
    if "鲜肉" in leading and "冻肉" in leading:
        return "鲜肉/冻肉为主"
    mapping = {"鲜肉": "鲜肉为主", "冻肉": "冻肉为主", "肉粉": "肉粉为主", "水解蛋白": "水解蛋白为主"}
    return mapping.get(forms[0], forms[0])


def _complexity(animal_sources: list[str], protein_items: list[dict[str, Any]]) -> str:
    source_count = len(animal_sources)
    if source_count <= 1:
        if len(protein_items) <= 1:
            return "单一来源"
        return "同类双源" if len(protein_items) == 2 else "同类多源"
    families = {str(item.get("ingredient_family") or "") for item in protein_items if item.get("ingredient_family")}
    if len(families) <= 1:
        return "同类双源" if source_count == 2 else "同类多源"
    return "跨类双源" if source_count == 2 else "跨类多源"


def _is_missing_protein_science(item: dict[str, Any]) -> bool:
    """Require protein science only when the standardized primary role says protein.

    ``is_protein`` is a legacy name-derived flag and is intentionally ignored here:
    legumes, organic minerals and palatability hydrolysates can carry that flag even
    when their authoritative nutrition role and science category are not protein.
    """
    if item.get("primary_nutrition_role") != "蛋白质供给":
        return False
    return not (
        item.get("science_status") == "active"
        and item.get("science_nutrition_category") == "protein"
    )


def load_rows(formula_id: int | None = None) -> list[dict[str, Any]]:
    cfg = get_mysql_config()
    with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=True) as conn:
        with conn.cursor() as cursor:
            sql = """
                SELECT fi.formula_id, fi.source_id, fi.brand, fi.product_name,
                       fi.nutrition_json, f.ingredient_fingerprint,
                       p.profile_version, p.input_hash,
                       i.position, i.raw_name, i.standard_ingredient_id, i.standard_name, i.ingredient_family,
                       i.source_type, i.animal_source, i.primary_nutrition_role,
                       i.protein_form, i.is_protein, i.is_plant_protein, i.features_json,
                       sp.nutrition_category AS science_nutrition_category,
                       sp.domain_attributes_json AS science_domain_attributes_json,
                       sp.science_status, sp.profile_version AS science_profile_version
                FROM catfood_formula_feature_input fi
                JOIN catfood_standard_formula f ON f.formula_id=fi.formula_id
                JOIN catfood_formula_feature_profile p ON p.formula_id=fi.formula_id
                LEFT JOIN catfood_formula_ingredient_item i
                  ON i.formula_id=fi.formula_id AND i.is_ignored=0
                LEFT JOIN catfood_ingredient_science_profile sp
                  ON sp.standard_ingredient_id=i.standard_ingredient_id
                WHERE p.overall_status='ready_for_rebuild'
                  AND (%s IS NULL OR fi.formula_id=%s)
                ORDER BY fi.formula_id, i.position
                """
            cursor.execute(sql, (formula_id, formula_id))
            source_rows = list(cursor.fetchall())
            cursor.execute("""SELECT rank_start,rank_end,position_weight,is_unknown
              FROM catfood_score_position_weight_config
              WHERE active=1 AND config_version='v1' AND domain_code='global'
              ORDER BY is_unknown,rank_start""")
            position_rows = cursor.fetchall()
    position_rules = [
        (int(row["rank_start"]), int(row["rank_end"]), float(row["position_weight"]))
        for row in position_rows if not row["is_unknown"]
    ] or DEFAULT_POSITION_WEIGHTS
    unknown_weight = next((float(row["position_weight"]) for row in position_rows if row["is_unknown"]), 0.5)

    grouped: dict[int, dict[str, Any]] = {}
    for source in source_rows:
        formula_id = int(source["formula_id"])
        group = grouped.setdefault(formula_id, {"source": source, "items": []})
        if source.get("position") is not None:
            group["items"].append(source)

    rows = []
    for formula_id, group in grouped.items():
        source = group["source"]
        items = group["items"]
        protein_items = [
            item for item in items
            if item.get("science_status") == "active"
            and item.get("science_nutrition_category") == "protein"
        ]
        missing_science = [
            item for item in items
            if _is_missing_protein_science(item)
        ]
        for item in protein_items:
            item["science_domain_attributes"] = _json(
                item.get("science_domain_attributes_json"), {}
            )
        animal_items = [item for item in protein_items if item.get("source_type") != "plant"]
        plant_items = [item for item in protein_items if item.get("source_type") == "plant"]
        animal_sources = list(dict.fromkeys(str(item.get("animal_source") or "").strip() for item in animal_items if str(item.get("animal_source") or "").strip()))
        form_labels = {
            "fresh": "鲜肉", "frozen": "冻肉", "meal": "肉粉",
            "hydrolyzed": "水解蛋白", "concentrate": "浓缩蛋白",
            "isolate": "分离蛋白", "other": "其他蛋白",
        }
        forms = [
            form_labels.get(str(item["science_domain_attributes"].get("protein_form") or ""))
            for item in animal_items
        ]
        forms = [form for form in forms if form]
        main_form = _main_form(forms)
        secondary_forms = list(dict.fromkeys(forms[1:]))
        plant_forms = [
            str(item["science_domain_attributes"].get("plant_protein_form") or "")
            for item in plant_items
        ]
        if not plant_items:
            plant_interference = "无植物蛋白"
        elif len(plant_items) > 1:
            plant_interference = "3级｜多源植物蛋白补强型"
        elif any(form in {"concentrate", "isolate"} for form in plant_forms):
            plant_interference = "2级｜单一高浓缩型植物蛋白"
        else:
            plant_interference = "1级｜单一温和型植物蛋白"
        hydrolyzed_count = sum(form == "水解蛋白" for form in forms)
        hydrolyzed_role = "主要出现" if forms and forms[0] == "水解蛋白" else "少量辅助" if hydrolyzed_count else None
        protein_weights = [_position_weight(item.get("position"), position_rules, unknown_weight) for item in protein_items]
        animal_weights = [_position_weight(item.get("position"), position_rules, unknown_weight) for item in animal_items]
        plant_weights = [_position_weight(item.get("position"), position_rules, unknown_weight) for item in plant_items]
        _, metric_value, _ = _crude_protein(source.get("nutrition_json"))
        rows.append(
            {
                "formula_id": formula_id,
                "source_id": source.get("source_id") or formula_id,
                "product_key": f"{source.get('brand') or ''}||{source.get('product_name') or ''}",
                "brand_name": source.get("brand"),
                "product_name": source.get("product_name"),
                "animal_sources": _join(animal_sources),
                "animal_source_level1_categories": _join(list(dict.fromkeys(item.get("ingredient_family") for item in animal_items if item.get("ingredient_family")))),
                "animal_source_level2_sources": _join(animal_sources),
                "protein_source_details": _join([item.get("standard_name") or item.get("raw_name") for item in protein_items]),
                "primary_meat_source_type": main_form,
                "secondary_meat_source_type": _join(secondary_forms),
                "meat_source_complexity": _complexity(animal_sources, animal_items),
                "plant_protein_labels": _join([item.get("standard_name") or item.get("raw_name") for item in plant_items]),
                "plant_protein_interference": plant_interference,
                "hydrolyzed_protein_role": hydrolyzed_role,
                "protein_position_weight": sum(protein_weights) / len(protein_weights) if protein_weights else unknown_weight,
                "main_protein_position_weight": animal_weights[0] if animal_weights else unknown_weight,
                "secondary_protein_position_weight": sum(animal_weights[1:]) / len(animal_weights[1:]) if len(animal_weights) > 1 else unknown_weight,
                "plant_protein_position_weight": sum(plant_weights) / len(plant_weights) if plant_weights else unknown_weight,
                "profile_status": "needs_review" if missing_science else "ready",
                "profile_version": "science-v1",
                "guarantee_crude_protein_value": metric_value,
                "source_fingerprint": hashlib.sha256(json.dumps(sorted(
                    (str(item.get("standard_ingredient_id") or ""), item.get("science_profile_version"))
                    for item in protein_items
                ), ensure_ascii=False, default=str).encode("utf-8")).hexdigest(),
            }
        )
    return rows


def _ddl(table: str) -> str:
    return f"""
        CREATE TABLE `{table}` (
          formula_id BIGINT UNSIGNED NOT NULL,
          source_id VARCHAR(100) NULL,
          product_key VARCHAR(600) NULL,
          brand_name VARCHAR(255) NULL,
          product_name VARCHAR(255) NULL,
          animal_sources TEXT,
          animal_source_level1_categories VARCHAR(255) DEFAULT NULL,
          animal_source_level2_sources TEXT,
          protein_source_details TEXT,
          primary_meat_source_type VARCHAR(255) DEFAULT NULL,
          secondary_meat_source_type VARCHAR(255) DEFAULT NULL,
          meat_source_complexity VARCHAR(255) DEFAULT NULL,
          plant_protein_labels TEXT,
          plant_protein_interference VARCHAR(255) DEFAULT NULL,
          hydrolyzed_protein_role VARCHAR(255) DEFAULT NULL,
          protein_position_weight DECIMAL(8,4) DEFAULT NULL,
          main_protein_position_weight DECIMAL(8,4) DEFAULT NULL,
          secondary_protein_position_weight DECIMAL(8,4) DEFAULT NULL,
          plant_protein_position_weight DECIMAL(8,4) DEFAULT NULL,
          profile_status VARCHAR(32) DEFAULT NULL,
          profile_version VARCHAR(32) DEFAULT NULL,
          guarantee_crude_protein_value DECIMAL(8,2) DEFAULT NULL,
          source_fingerprint CHAR(64) DEFAULT NULL,
          updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (formula_id),
          KEY idx_profile_status (profile_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """


def write_rows(rows: list[dict[str, Any]], *, keep_backup: bool) -> dict[str, Any]:
    cfg = get_feature_mysql_config()
    stage = f"{TARGET_TABLE}_stage_profile"
    backup = f"{TARGET_TABLE}_bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    columns = list(rows[0]) if rows else []
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = (
        f"INSERT INTO `{stage}` ({', '.join(f'`{name}`' for name in columns)}) "
        f"VALUES ({placeholders})"
    )
    with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS `{stage}`")
            cursor.execute(_ddl(stage))
            if rows:
                cursor.executemany(insert_sql, [tuple(row[name] for name in columns) for row in rows])
            cursor.execute(f"SELECT COUNT(*) n, COUNT(DISTINCT formula_id) formulas FROM `{stage}`")
            check = cursor.fetchone()
            if int(check["n"]) != len(rows) or int(check["formulas"]) != len(rows):
                raise RuntimeError(f"profile projection validation failed: {check}")
            cursor.execute("SHOW TABLES LIKE %s", (TARGET_TABLE,))
            target_exists = bool(cursor.fetchone())
            if target_exists:
                cursor.execute(f"RENAME TABLE `{TARGET_TABLE}` TO `{backup}`, `{stage}` TO `{TARGET_TABLE}`")
                if not keep_backup:
                    cursor.execute(f"DROP TABLE `{backup}`")
                    backup = None
            else:
                cursor.execute(f"RENAME TABLE `{stage}` TO `{TARGET_TABLE}`")
                backup = None
        conn.commit()
    return {"written": len(rows), "backup_table": backup}


def upsert_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"written": 0, "backup_table": None}
    cfg = get_feature_mysql_config()
    columns = list(rows[0])
    update_columns = [name for name in columns if name != "formula_id"]
    sql = (
        f"INSERT INTO `{TARGET_TABLE}` ({', '.join(f'`{name}`' for name in columns)}) "
        f"VALUES ({', '.join(['%s'] * len(columns))}) ON DUPLICATE KEY UPDATE "
        + ", ".join(f"`{name}`=VALUES(`{name}`)" for name in update_columns)
    )
    with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SHOW COLUMNS FROM `{TARGET_TABLE}`")
            existing = {row["Field"] for row in cursor.fetchall()}
            for name in ("protein_position_weight", "main_protein_position_weight", "secondary_protein_position_weight", "plant_protein_position_weight"):
                if name not in existing:
                    cursor.execute(f"ALTER TABLE `{TARGET_TABLE}` ADD COLUMN `{name}` DECIMAL(8,4) NULL")
            cursor.executemany(sql, [tuple(row[name] for name in columns) for row in rows])
        conn.commit()
    return {"written": len(rows), "backup_table": None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="atomically replace target table")
    parser.add_argument("--keep-backup", action="store_true")
    parser.add_argument("--formula-id", type=int, default=None)
    args = parser.parse_args()
    rows = load_rows(args.formula_id)
    summary = {
        "ok": True,
        "applied": bool(args.apply),
        "row_count": len(rows),
        "profile_status": {
            status: sum(row.get("profile_status") == status for row in rows)
            for status in ("ready", "needs_review", "ready_with_warnings", "blocked", None)
        },
        "with_crude_protein": sum(row.get("guarantee_crude_protein_value") is not None for row in rows),
        "missing_main_protein_form": sum(not row.get("primary_meat_source_type") for row in rows),
        "preview": rows[:3],
    }
    if args.apply:
        summary.update(
            upsert_rows(rows) if args.formula_id is not None
            else write_rows(rows, keep_backup=bool(args.keep_backup))
        )
    print(json.dumps(summary, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
