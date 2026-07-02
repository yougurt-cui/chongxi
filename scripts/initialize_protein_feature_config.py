#!/usr/bin/env python3
"""Create and seed normalized protein labels, matching rules, and score config."""

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


LABEL_TABLE = "catfood_feature_label"
RULE_TABLE = "catfood_ingredient_feature_rule"
LABEL_SCORE_TABLE = "catfood_label_score_config"
WEIGHT_TABLE = "catfood_score_weight_config"
RULE_VIEW = "catfood_ingredient_feature_rule_view"
CONFIG_VERSION = "protein_v1"


LABELS = [
    # Ingredient-level atomic labels used by matching rules.
    ("ingredient_protein_form", "蛋白形态", "fresh_meat", "鲜肉", "原料以鲜肉或新鲜肉形式出现"),
    ("ingredient_protein_form", "蛋白形态", "frozen_meat", "冻肉", "原料以非冻干的冷冻肉形式出现"),
    ("ingredient_protein_form", "蛋白形态", "meat_meal", "肉粉", "原料以肉粉、鱼粉或虾粉形式出现"),
    ("ingredient_protein_form", "蛋白形态", "hydrolyzed_protein", "水解蛋白", "原料经过水解处理"),
    ("ingredient_plant_protein_class", "植物蛋白类型", "concentrated", "高浓缩型植物蛋白", "蛋白粉、谷朊粉等高浓缩植物蛋白来源"),
    # Formula-level business labels.
    ("meat_source_complexity", "肉源复杂度", "single_meat", "单一肉源", "配方仅包含一个明确肉源"),
    ("meat_source_complexity", "肉源复杂度", "single_source", "单一来源", "配方仅包含一个明确动物来源"),
    ("meat_source_complexity", "肉源复杂度", "same_class_double", "同类双源", "同一动物大类中包含两个来源"),
    ("meat_source_complexity", "肉源复杂度", "same_class_multi", "同类多源", "同一动物大类中包含三个及以上来源"),
    ("meat_source_complexity", "肉源复杂度", "cross_class_double", "跨类双源", "跨动物大类包含两个来源"),
    ("meat_source_complexity", "肉源复杂度", "cross_class_multi", "跨类多源", "跨动物大类包含三个及以上来源"),
    ("main_protein_form", "主要蛋白形态", "fresh_main", "鲜肉为主", "首要动物蛋白形态为鲜肉"),
    ("main_protein_form", "主要蛋白形态", "frozen_main", "冻肉为主", "首要动物蛋白形态为冻肉"),
    ("main_protein_form", "主要蛋白形态", "fresh_frozen_main", "鲜肉/冻肉为主", "配方前部同时以鲜肉和冻肉为主要形态"),
    ("main_protein_form", "主要蛋白形态", "meal_main", "肉粉为主", "首要动物蛋白形态为肉粉"),
    ("main_protein_form", "主要蛋白形态", "hydrolyzed_main", "水解蛋白为主", "首要蛋白形态为水解蛋白"),
    ("secondary_protein_form", "次要蛋白形态", "none", "无", "没有可识别的次要蛋白形态"),
    ("secondary_protein_form", "次要蛋白形态", "fresh", "鲜肉", "次要蛋白中包含鲜肉"),
    ("secondary_protein_form", "次要蛋白形态", "frozen", "冻肉", "次要蛋白中包含冻肉"),
    ("secondary_protein_form", "次要蛋白形态", "fresh_frozen", "鲜肉/冻肉", "次要蛋白中同时包含鲜肉和冻肉"),
    ("secondary_protein_form", "次要蛋白形态", "meal", "肉粉", "次要蛋白中包含肉粉"),
    ("secondary_protein_form", "次要蛋白形态", "hydrolyzed", "水解蛋白", "次要蛋白中包含水解蛋白"),
    ("plant_protein_interference", "植物蛋白干扰", "none", "无植物蛋白", "未识别到植物蛋白参与"),
    ("plant_protein_interference", "植物蛋白干扰", "mild_single", "1级｜单一温和型植物蛋白", "单一、非高浓缩植物蛋白来源"),
    ("plant_protein_interference", "植物蛋白干扰", "concentrated_single", "2级｜单一高浓缩型植物蛋白", "单一高浓缩植物蛋白来源"),
    ("plant_protein_interference", "植物蛋白干扰", "concentrated_multi", "3级｜多源植物蛋白补强型", "两个及以上高浓缩植物蛋白来源"),
    ("plant_protein_interference", "植物蛋白干扰", "legume_dominant", "4级｜豆类主导混合型植物蛋白", "豆类主导且包含多种植物蛋白来源"),
    ("hydrolyzed_protein_role", "水解蛋白角色", "none", "无", "配方未识别到水解蛋白"),
    ("hydrolyzed_protein_role", "水解蛋白角色", "secondary", "次要出现", "水解蛋白出现在次要蛋白位置"),
    ("hydrolyzed_protein_role", "水解蛋白角色", "main", "主要出现", "水解蛋白为主要蛋白形态"),
    ("animal_source_category", "动物来源分类", "poultry", "禽类", "鸡、鸭、火鸡、鹅、鹌鹑或鸽等禽类来源"),
    ("animal_source_category", "动物来源分类", "fish", "鱼类", "鱼类或海洋动物蛋白来源"),
    ("animal_source_category", "动物来源分类", "red_meat", "红肉类", "牛、羊、鹿、兔或猪等来源"),
    ("animal_source_category", "动物来源分类", "egg", "蛋类", "鸡蛋、鸭蛋等蛋类来源"),
]


LABEL_SCORES = {
    "meat_source_complexity": {
        "单一肉源": 1.0, "单一来源": 1.0, "同类双源": 2.0,
        "同类多源": 3.0, "跨类双源": 4.0, "跨类多源": 5.0,
    },
    "main_protein_form": {
        "鲜肉为主": 1.0, "冻肉为主": 1.5, "鲜肉/冻肉为主": 1.25,
        "肉粉为主": 2.0, "水解蛋白为主": 0.5,
    },
    "secondary_protein_form": {
        "无": 0.0, "鲜肉": 0.5, "冻肉": 0.5, "鲜肉/冻肉": 0.5,
        "肉粉": 1.0, "水解蛋白": 0.0,
    },
    "plant_protein_interference": {
        "无植物蛋白": 0.0, "1级｜单一温和型植物蛋白": 0.25,
        "2级｜单一高浓缩型植物蛋白": 0.5,
        "3级｜多源植物蛋白补强型": 0.75,
        "4级｜豆类主导混合型植物蛋白": 1.0,
    },
    "hydrolyzed_protein_role": {"无": 0.0, "次要出现": 0.5, "主要出现": 1.0},
}


QUALITY_WEIGHTS = [
    ("animal_protein_dominance", "动物蛋白占优", 0.30, "positive"),
    ("source_clarity", "肉源清晰度", 0.25, "positive"),
    ("digestibility", "蛋白形态消化支持", 0.20, "positive"),
    ("low_plant_interference", "低植物蛋白干扰", 0.15, "positive"),
    ("protein_content_suitability", "粗蛋白含量适配度", 0.10, "positive"),
]

# Six values use 0.142857 and one uses 0.142858 so DECIMAL(10,6) sums to 1.
PRESSURE_WEIGHTS = [
    ("meat_source_complexity_score", "肉源复杂度", 0.142858, "pressure"),
    ("main_protein_form_score", "主要蛋白形态", 0.142857, "pressure"),
    ("secondary_protein_form_score", "次要蛋白形态", 0.142857, "pressure"),
    ("form_mix_complexity_score", "蛋白形态混合复杂度", 0.142857, "pressure"),
    ("hydrolyzed_protein_load_score", "水解蛋白负载", 0.142857, "pressure"),
    ("plant_protein_interference_score", "植物蛋白干扰", 0.142857, "pressure"),
    ("protein_content_score", "粗蛋白含量", 0.142857, "pressure"),
]


INGREDIENT_RULES = [
    ("raw_name", "水解", "contains", None, "ingredient_protein_form", "hydrolyzed_protein", 330, 1.0),
    ("raw_name", "磷虾粉", "contains", None, "ingredient_protein_form", "meat_meal", 325, 1.0),
    ("raw_name", "肉粉", "contains", None, "ingredient_protein_form", "meat_meal", 320, 1.0),
    ("raw_name", "鱼粉", "contains", None, "ingredient_protein_form", "meat_meal", 320, 1.0),
    ("raw_name", "虾粉", "contains", None, "ingredient_protein_form", "meat_meal", 320, 1.0),
    ("raw_name", "冻干", "contains", None, "ingredient_protein_form", "fresh_meat", 315, 0.9),
    ("raw_name", "冻", "contains", "冻干", "ingredient_protein_form", "frozen_meat", 300, 0.95),
    ("raw_name", "新鲜", "contains", None, "ingredient_protein_form", "fresh_meat", 305, 0.98),
    ("raw_name", "鲜", "contains", None, "ingredient_protein_form", "fresh_meat", 300, 0.95),
]

for term in ("玉米蛋白粉", "大米蛋白粉", "豌豆蛋白", "马铃薯蛋白", "小麦蛋白", "谷朊粉", "豆粕", "大豆蛋白"):
    INGREDIENT_RULES.append(
        ("raw_name", term, "contains", None, "ingredient_plant_protein_class", "concentrated", 300, 1.0)
    )


def _connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def _ensure_rule_columns(cursor) -> None:
    cursor.execute(f"SHOW COLUMNS FROM `{RULE_TABLE}`")
    existing = {row["Field"] for row in cursor.fetchall()}
    additions = {
        "label_id": "BIGINT UNSIGNED NULL AFTER rule_id",
        "rule_stage": "VARCHAR(32) NOT NULL DEFAULT 'ingredient' AFTER label_id",
        "match_operator": "VARCHAR(32) NOT NULL DEFAULT 'contains' AFTER match_scope",
        "exclude_value": "VARCHAR(255) NULL AFTER match_value",
        "rule_version": "VARCHAR(32) NOT NULL DEFAULT 'v1' AFTER rule_note",
    }
    for column, ddl in additions.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE `{RULE_TABLE}` ADD COLUMN `{column}` {ddl}")


def ensure_config_schema(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{LABEL_TABLE}` (
          label_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          feature_domain VARCHAR(64) NOT NULL,
          dimension_code VARCHAR(128) NOT NULL,
          dimension_name VARCHAR(128) NOT NULL,
          value_code VARCHAR(128) NOT NULL,
          value_name VARCHAR(255) NOT NULL,
          label_content TEXT NOT NULL,
          config_version VARCHAR(32) NOT NULL,
          active TINYINT NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (label_id),
          UNIQUE KEY uq_feature_label (feature_domain, dimension_code, value_code, config_version),
          KEY idx_feature_label_active (feature_domain, dimension_code, active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{LABEL_SCORE_TABLE}` (
          config_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          label_id BIGINT UNSIGNED NOT NULL,
          score_code VARCHAR(128) NOT NULL,
          component_code VARCHAR(128) NOT NULL,
          score_value DECIMAL(10,5) NOT NULL,
          config_version VARCHAR(32) NOT NULL,
          active TINYINT NOT NULL DEFAULT 1,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (config_id),
          UNIQUE KEY uq_label_score (label_id, score_code, component_code, config_version),
          KEY idx_label_score_active (score_code, active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{WEIGHT_TABLE}` (
          weight_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          score_code VARCHAR(128) NOT NULL,
          component_code VARCHAR(128) NOT NULL,
          component_name VARCHAR(255) NOT NULL,
          weight DECIMAL(10,6) NOT NULL,
          direction VARCHAR(32) NOT NULL DEFAULT 'positive',
          config_version VARCHAR(32) NOT NULL,
          active TINYINT NOT NULL DEFAULT 1,
          rule_note TEXT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (weight_id),
          UNIQUE KEY uq_score_weight (score_code, component_code, config_version),
          KEY idx_score_weight_active (score_code, active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_rule_columns(cursor)


def seed_protein_config(cursor) -> dict[str, int]:
    ensure_config_schema(cursor)
    for dimension_code, dimension_name, value_code, value_name, content in LABELS:
        cursor.execute(
            f"""
            INSERT INTO `{LABEL_TABLE}`(
              feature_domain, dimension_code, dimension_name, value_code,
              value_name, label_content, config_version, active
            ) VALUES('protein', %s, %s, %s, %s, %s, %s, 1)
            ON DUPLICATE KEY UPDATE
              dimension_name=VALUES(dimension_name), value_name=VALUES(value_name),
              label_content=VALUES(label_content), active=1
            """,
            (dimension_code, dimension_name, value_code, value_name, content, CONFIG_VERSION),
        )
    cursor.execute(
        f"SELECT label_id, dimension_code, value_code, value_name FROM `{LABEL_TABLE}` "
        "WHERE feature_domain='protein' AND config_version=%s",
        (CONFIG_VERSION,),
    )
    labels = {(row["dimension_code"], row["value_code"]): row for row in cursor.fetchall()}

    # Disable only legacy seed rows; retain them for audit and leave manual rules untouched.
    cursor.execute(
        f"""
        UPDATE `{RULE_TABLE}` SET active=0
        WHERE feature_domain='protein' AND label_id IS NULL
          AND (
            rule_note='seeded for protein standardization compatibility'
            OR rule_note IN (
              '标准配料营养角色：动物蛋白', '标准配料营养角色：植物蛋白',
              '原料名称蛋白形态兜底', '兼容现有评分口径：冻干归入鲜肉形态',
              '显式蛋白原料关键词', '高浓缩植物蛋白关键词'
            )
          )
        """
    )
    for scope, match_value, operator, exclude_value, dimension, value_code, priority, confidence in INGREDIENT_RULES:
        label = labels[(dimension, value_code)]
        cursor.execute(
            f"""
            INSERT INTO `{RULE_TABLE}`(
              label_id, rule_stage, match_scope, match_operator, match_value,
              exclude_value, feature_domain, feature_key, feature_value,
              priority, confidence, active, rule_note, rule_version
            ) VALUES(%s, 'ingredient', %s, %s, %s, %s, 'protein', %s, %s,
                     %s, %s, 1, 'seed:protein-config-v1', %s)
            ON DUPLICATE KEY UPDATE
              label_id=VALUES(label_id), rule_stage=VALUES(rule_stage),
              match_operator=VALUES(match_operator), exclude_value=VALUES(exclude_value),
              priority=VALUES(priority), confidence=VALUES(confidence), active=1,
              rule_note=VALUES(rule_note), rule_version=VALUES(rule_version)
            """,
            (label["label_id"], scope, operator, match_value, exclude_value,
             dimension, value_code, priority, confidence, CONFIG_VERSION),
        )

    for dimension, score_map in LABEL_SCORES.items():
        for value_name, score_value in score_map.items():
            label = next(row for row in labels.values() if row["dimension_code"] == dimension and row["value_name"] == value_name)
            cursor.execute(
                f"""
                INSERT INTO `{LABEL_SCORE_TABLE}`(
                  label_id, score_code, component_code, score_value, config_version, active
                ) VALUES(%s, 'protein_structure_score', %s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE score_value=VALUES(score_value), active=1
                """,
                (label["label_id"], dimension, score_value, CONFIG_VERSION),
            )

    for score_code, weight_rows in (
        ("protein_quality_score", QUALITY_WEIGHTS),
        ("protein_structure_score", PRESSURE_WEIGHTS),
    ):
        for component_code, component_name, weight, direction in weight_rows:
            cursor.execute(
                f"""
                INSERT INTO `{WEIGHT_TABLE}`(
                  score_code, component_code, component_name, weight,
                  direction, config_version, active, rule_note
                ) VALUES(%s, %s, %s, %s, %s, %s, 1, 'seed:protein-config-v1')
                ON DUPLICATE KEY UPDATE
                  component_name=VALUES(component_name), weight=VALUES(weight),
                  direction=VALUES(direction), active=1, rule_note=VALUES(rule_note)
                """,
                (score_code, component_code, component_name, weight, direction, CONFIG_VERSION),
            )

    cursor.execute(
        f"""
        CREATE OR REPLACE VIEW `{RULE_VIEW}` AS
        SELECT r.rule_id, r.rule_stage, r.match_scope, r.match_operator,
               r.match_value, r.exclude_value, r.priority, r.confidence,
               r.active, r.rule_version, l.label_id, l.feature_domain,
               l.dimension_code, l.dimension_name, l.value_code,
               l.value_name, l.label_content, l.config_version
        FROM `{RULE_TABLE}` r
        LEFT JOIN `{LABEL_TABLE}` l ON l.label_id=r.label_id
        """
    )
    return {"labels": len(LABELS), "rules": len(INGREDIENT_RULES),
            "label_scores": sum(len(values) for values in LABEL_SCORES.values()),
            "quality_weights": len(QUALITY_WEIGHTS),
            "pressure_weights": len(PRESSURE_WEIGHTS)}


def initialize(*, apply: bool) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            result = seed_protein_config(cursor)
        conn.commit() if apply else conn.rollback()
    return {"ok": True, "applied": apply, "version": CONFIG_VERSION, **result}


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize normalized protein feature configuration.")
    parser.add_argument("--apply", action="store_true", help="commit changes; default rolls back")
    args = parser.parse_args()
    print(json.dumps(initialize(apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
