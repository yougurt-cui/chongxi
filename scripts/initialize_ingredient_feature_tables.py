#!/usr/bin/env python3
"""Create optional ingredient item/profile/rule tables for formula feature modeling."""

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


INGREDIENT_CANDIDATE_TABLE = "catfood_standard_ingredient_candidate"
INGREDIENT_FEATURE_RULE_TABLE = "catfood_ingredient_feature_rule"
FORMULA_INGREDIENT_ITEM_TABLE = "catfood_formula_ingredient_item"
FORMULA_FEATURE_PROFILE_TABLE = "catfood_formula_feature_profile"


def _connect():
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def ensure_tables(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{INGREDIENT_CANDIDATE_TABLE}` (
          candidate_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          raw_name VARCHAR(255) NOT NULL,
          normalized_raw_name VARCHAR(255) NOT NULL,
          context LONGTEXT NULL,
          suggested_standard_ingredient_id VARCHAR(64) NULL,
          suggested_standard_name VARCHAR(255) NULL,
          model_result_json JSON NULL,
          status VARCHAR(32) NOT NULL DEFAULT 'pending',
          reviewer VARCHAR(128) NULL,
          review_note TEXT NULL,
          reviewed_at DATETIME NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (candidate_id),
          UNIQUE KEY uq_ingredient_candidate_normalized (normalized_raw_name),
          KEY idx_ingredient_candidate_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{INGREDIENT_FEATURE_RULE_TABLE}` (
          rule_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          match_scope VARCHAR(64) NOT NULL,
          match_value VARCHAR(255) NOT NULL,
          feature_domain VARCHAR(64) NOT NULL,
          feature_key VARCHAR(128) NOT NULL,
          feature_value VARCHAR(255) NOT NULL,
          priority INT NOT NULL DEFAULT 100,
          confidence DECIMAL(6,5) NOT NULL DEFAULT 1.00000,
          active TINYINT NOT NULL DEFAULT 1,
          rule_note TEXT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (rule_id),
          UNIQUE KEY uq_ingredient_feature_rule (
            match_scope, match_value, feature_domain, feature_key, feature_value
          ),
          KEY idx_ingredient_feature_domain (feature_domain, active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{FORMULA_INGREDIENT_ITEM_TABLE}` (
          item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          formula_id BIGINT UNSIGNED NOT NULL,
          position INT NOT NULL,
          raw_name VARCHAR(255) NOT NULL,
          standard_ingredient_id VARCHAR(64) NULL,
          standard_name VARCHAR(255) NULL,
          ingredient_family VARCHAR(128) NULL,
          source_type VARCHAR(64) NULL,
          animal_source VARCHAR(128) NULL,
          primary_nutrition_role VARCHAR(128) NULL,
          protein_form VARCHAR(64) NULL,
          modifiers_json JSON NULL,
          match_method VARCHAR(64) NULL,
          confidence DECIMAL(6,5) NULL,
          is_protein TINYINT NOT NULL DEFAULT 0,
          is_plant_protein TINYINT NOT NULL DEFAULT 0,
          features_json JSON NULL,
          match_status VARCHAR(32) NULL,
          review_status VARCHAR(32) NULL,
          issue_severity VARCHAR(32) NULL,
          affected_domains_json JSON NULL,
          is_ignored TINYINT NOT NULL DEFAULT 0,
          review_reason VARCHAR(255) NULL,
          reviewer VARCHAR(128) NULL,
          reviewed_at DATETIME NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (item_id),
          UNIQUE KEY uq_formula_ingredient_position (formula_id, position),
          KEY idx_formula_ingredient_formula (formula_id),
          KEY idx_formula_ingredient_standard (standard_ingredient_id),
          KEY idx_formula_ingredient_protein (formula_id, is_protein)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{FORMULA_FEATURE_PROFILE_TABLE}` (
          formula_id BIGINT UNSIGNED NOT NULL,
          ingredient_fingerprint CHAR(64) NOT NULL,
          ingredient_count INT NOT NULL DEFAULT 0,
          effective_ingredient_count INT NOT NULL DEFAULT 0,
          standardized_ingredient_count INT NOT NULL DEFAULT 0,
          ignored_ingredient_count INT NOT NULL DEFAULT 0,
          unmatched_ingredient_count INT NOT NULL DEFAULT 0,
          blocking_ingredient_count INT NOT NULL DEFAULT 0,
          warning_ingredient_count INT NOT NULL DEFAULT 0,
          standardization_coverage DECIMAL(8,5) NOT NULL DEFAULT 0,
          overall_status VARCHAR(32) NOT NULL,
          protein_status VARCHAR(32) NOT NULL,
          fat_status VARCHAR(32) NOT NULL,
          fiber_status VARCHAR(32) NOT NULL,
          starch_status VARCHAR(32) NOT NULL,
          domain_gate_json JSON NOT NULL,
          dirty_domains_json JSON NOT NULL,
          quality_metrics_json JSON NOT NULL,
          input_hash CHAR(64) NOT NULL,
          profile_version VARCHAR(32) NOT NULL DEFAULT 'gate-v1',
          rule_versions_json JSON NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (formula_id),
          KEY idx_formula_feature_profile_version (profile_version),
          KEY idx_formula_feature_profile_status (overall_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def seed_protein_rules(cursor) -> int:
    # Keep the legacy CLI entry point, but seed the normalized label-backed rules.
    from scripts.initialize_protein_feature_config import seed_protein_config

    return seed_protein_config(cursor)["rules"]


def initialize_tables(*, apply: bool, seed_rules: bool) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            ensure_tables(cursor)
            seeded = seed_protein_rules(cursor) if seed_rules else 0
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return {
        "ok": True,
        "applied": apply,
        "seed_rules": seed_rules,
        "seeded_rules": seeded,
        "tables": [
            INGREDIENT_CANDIDATE_TABLE,
            INGREDIENT_FEATURE_RULE_TABLE,
            FORMULA_INGREDIENT_ITEM_TABLE,
            FORMULA_FEATURE_PROFILE_TABLE,
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize ingredient feature modeling tables.")
    parser.add_argument("--apply", action="store_true", help="write changes; default rolls back")
    parser.add_argument("--seed-rules", action="store_true", help="seed initial protein feature rules")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(json.dumps(initialize_tables(apply=args.apply, seed_rules=args.seed_rules), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
