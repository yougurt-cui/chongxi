#!/usr/bin/env python3
"""Replace legacy feature-source tables with formula-keyed compact tables."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config, get_mysql_config


TABLE_DDLS = {
    "protein_source_aggregate": """
        CREATE TABLE `{table}` (
          formula_id BIGINT UNSIGNED NOT NULL,
          animal_sources TEXT NULL,
          animal_source_level1_categories VARCHAR(255) NULL,
          animal_source_level2_sources TEXT NULL,
          protein_source_details TEXT NULL,
          primary_meat_source_type VARCHAR(255) NULL,
          secondary_meat_source_type VARCHAR(255) NULL,
          meat_source_complexity VARCHAR(255) NULL,
          plant_protein_labels TEXT NULL,
          plant_protein_interference VARCHAR(255) NULL,
          hydrolyzed_protein_role VARCHAR(255) NULL,
          guarantee_crude_protein_value DECIMAL(8,2) NULL,
          profile_status VARCHAR(32) NULL,
          profile_version VARCHAR(32) NULL,
          source_fingerprint CHAR(64) NULL,
          updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (formula_id),
          KEY idx_profile_status (profile_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "catfood_fiber_feature_json": """
        CREATE TABLE `{table}` (
          formula_id BIGINT UNSIGNED NOT NULL,
          raw_ingredient_text LONGTEXT NULL,
          ingredient_feature_json JSON NOT NULL,
          starch_ingredients_json JSON NULL,
          profile_status VARCHAR(32) NULL,
          profile_version VARCHAR(32) NULL,
          source_fingerprint CHAR(64) NULL,
          updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (formula_id),
          KEY idx_profile_status (profile_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "catfood_fat_material_features": """
        CREATE TABLE `{table}` (
          formula_id BIGINT UNSIGNED NOT NULL,
          ingredient_composition LONGTEXT NULL,
          fat_sources TEXT NULL,
          fat_source_types VARCHAR(500) NULL,
          antioxidant_sources TEXT NULL,
          antioxidant_types VARCHAR(500) NULL,
          micronutrient_sources TEXT NULL,
          micronutrient_types VARCHAR(500) NULL,
          omega6_sources TEXT NULL,
          omega3_sources TEXT NULL,
          guarantee_crude_fat_value DECIMAL(18,2) NULL,
          guarantee_crude_fat_operator VARCHAR(10) NULL,
          guarantee_crude_fat_basis VARCHAR(50) NULL,
          profile_status VARCHAR(32) NULL,
          profile_version VARCHAR(32) NULL,
          source_fingerprint CHAR(64) NULL,
          needs_review TINYINT NOT NULL DEFAULT 0,
          review_reason VARCHAR(255) NULL,
          updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (formula_id),
          KEY idx_profile_status (profile_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
}


TABLE_COLUMNS = {
    "protein_source_aggregate": (
        "formula_id", "animal_sources", "animal_source_level1_categories",
        "animal_source_level2_sources", "protein_source_details",
        "primary_meat_source_type", "secondary_meat_source_type",
        "meat_source_complexity", "plant_protein_labels",
        "plant_protein_interference", "hydrolyzed_protein_role",
        "guarantee_crude_protein_value",
    ),
    "catfood_fiber_feature_json": (
        "formula_id", "raw_ingredient_text", "ingredient_feature_json",
        "starch_ingredients_json",
    ),
    "catfood_fat_material_features": (
        "formula_id", "ingredient_composition", "fat_sources", "fat_source_types",
        "antioxidant_sources", "antioxidant_types", "micronutrient_sources",
        "micronutrient_types", "omega6_sources", "omega3_sources",
        "guarantee_crude_fat_value", "guarantee_crude_fat_operator",
        "guarantee_crude_fat_basis", "needs_review", "review_reason",
    ),
}

ROW_ORDER = {
    "protein_source_aggregate": "aggregated_at DESC, source_id DESC",
    "catfood_fiber_feature_json": "updated_at DESC, id DESC",
    "catfood_fat_material_features": "updated_at DESC, id DESC",
}


def _database_name(config: dict) -> str:
    return str(config["database"])


def migrate(*, apply: bool) -> dict:
    feature_cfg = get_feature_mysql_config()
    standard_db = _database_name(get_mysql_config())
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = {"ok": True, "applied": apply, "standard_database": standard_db, "tables": {}}

    with pymysql.connect(**feature_cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            for table, columns in TABLE_COLUMNS.items():
                stage = f"{table}_stage_formula_v2"
                backup = f"{table}_bak_{timestamp}"
                cursor.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
                before = int(cursor.fetchone()["n"])
                cursor.execute(
                    f"SELECT COUNT(DISTINCT t.formula_id) AS n FROM `{table}` t "
                    f"JOIN `{standard_db}`.`catfood_standard_formula` f ON f.formula_id=t.formula_id"
                )
                retained = int(cursor.fetchone()["n"])
                table_summary = {
                    "before": before,
                    "retained": retained,
                    "removed": before - retained,
                    "backup_table": backup if apply else None,
                }
                summary["tables"][table] = table_summary
                if not apply:
                    continue

                cursor.execute(f"DROP TABLE IF EXISTS `{stage}`")
                cursor.execute(TABLE_DDLS[table].format(table=stage))
                quoted = ", ".join(f"`{column}`" for column in columns)
                select_columns = ", ".join(f"t.`{column}`" for column in columns)
                cursor.execute(
                    f"INSERT INTO `{stage}` ({quoted}, profile_status, profile_version, source_fingerprint) "
                    f"SELECT {select_columns}, "
                    f"COALESCE(JSON_UNQUOTE(JSON_EXTRACT(p.feature_profile_json, '$.overall_compare_status')), 'ready'), "
                    f"COALESCE(p.profile_version, 'v1'), f.ingredient_fingerprint "
                    f"FROM (SELECT source_rows.*, ROW_NUMBER() OVER ("
                    f"PARTITION BY source_rows.formula_id ORDER BY {ROW_ORDER[table]}) AS formula_rank "
                    f"FROM `{table}` source_rows) t "
                    f"JOIN `{standard_db}`.`catfood_standard_formula` f ON f.formula_id=t.formula_id "
                    f"LEFT JOIN `{standard_db}`.`catfood_formula_feature_profile` p ON p.formula_id=t.formula_id "
                    f"WHERE t.formula_rank=1"
                )
                cursor.execute(
                    f"SELECT COUNT(*) AS n, COUNT(DISTINCT formula_id) AS formulas FROM `{stage}`"
                )
                check = cursor.fetchone()
                if int(check["n"]) != retained or int(check["formulas"]) != retained:
                    raise RuntimeError(f"{table} migration validation failed: {check}")
                cursor.execute(
                    f"RENAME TABLE `{table}` TO `{backup}`, `{stage}` TO `{table}`"
                )
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(apply=bool(args.apply)), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
