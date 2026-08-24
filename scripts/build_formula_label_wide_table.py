# -*- coding: utf-8 -*-
"""Build a formula-level label wide table: catfood_formula_label_wide.

Replaces the old SKU-granularity catfood_sku_label_wide (212 columns)
with a lean formula-granularity table (~25 columns).

Base:    catfood_standard_formula  (csv_labeling, 125 rows)
Scores:  sku_feature_input          (protein_feature_platform, on formula_id)
         catfood_fiber_feature_score (protein_feature_platform, on formula_id)
         catfood_fat_material_features_scored (protein_feature_platform, on formula_id)
"""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pymysql

# =========================
# Database config
# =========================

DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

LABEL_DB = os.getenv("LABEL_SOURCE_DATABASE", "csv_labeling")
FEATURE_DB = os.getenv("MYSQL_DATABASE", "protein_feature_platform")
OUTPUT_TABLE = "catfood_formula_label_wide"

SCORE_DECIMALS = 4


# =========================
# Helpers
# =========================

def _connect(database: str) -> pymysql.Connection:
    cfg = {**DB_CONFIG, "database": database}
    return pymysql.connect(**cfg)


def _quote(name: str) -> str:
    if not re.match(r"^[A-Za-z0-9_]+$", name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return f"`{name}`"


def _fq(schema: str, table: str) -> str:
    return f"{_quote(schema)}.{_quote(table)}"


def _to_json(value: Any) -> Optional[str]:
    """Ensure value is a JSON string; pass through None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _to_decimal(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), SCORE_DECIMALS)
    except (TypeError, ValueError):
        return None


# =========================
# Data loading
# =========================

def load_formula_base() -> List[Dict[str, Any]]:
    """Load the base: formula + product + brand from csv_labeling."""
    conn = _connect(LABEL_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    f.formula_id,
                    f.product_id,
                    p.brand_id,
                    b.standard_brand_name,
                    p.standard_product_name,
                    f.raw_ingredient_example,
                    f.normalized_ingredient_composition,
                    f.normalized_ingredients_json,
                    f.nutrition_json,
                    p.life_stage,
                    p.product_type
                FROM {_fq(LABEL_DB, 'catfood_standard_formula')} f
                JOIN {_fq(LABEL_DB, 'catfood_standard_product')} p
                    ON p.product_id = f.product_id
                JOIN {_fq(LABEL_DB, 'catfood_standard_brand')} b
                    ON b.brand_id = p.brand_id
                ORDER BY f.formula_id
            """)
            return cur.fetchall()
    finally:
        conn.close()


def load_feature_scores() -> Dict[int, Dict[str, Any]]:
    """Load scores from sku_feature_input, deduplicated by formula_id (MAX)."""
    conn = _connect(FEATURE_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    formula_id,
                    MAX(protein_score)       AS protein_score,
                    MAX(carb_score)          AS carb_score,
                    MAX(fiber_score)         AS fiber_score,
                    MAX(fat_score)           AS fat_score,
                    MAX(prebiotic_score)     AS prebiotic_score,
                    MAX(antioxidant_score)   AS antioxidant_score,
                    MAX(p_buffer)            AS p_buffer,
                    MAX(q_feed)              AS q_feed,
                    MAX(q_scfa)              AS q_scfa
                FROM {_quote('sku_feature_input')}
                WHERE formula_id IS NOT NULL
                GROUP BY formula_id
            """)
            rows = cur.fetchall()
        return {int(r["formula_id"]): r for r in rows}
    finally:
        conn.close()


def load_fiber_scores() -> Dict[int, Dict[str, Any]]:
    """Load p_level / q_level from catfood_fiber_feature_score."""
    conn = _connect(FEATURE_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    formula_id,
                    p_level,
                    q_level
                FROM {_quote('catfood_fiber_feature_score')}
                WHERE formula_id IS NOT NULL
            """)
            rows = cur.fetchall()
        return {int(r["formula_id"]): r for r in rows}
    finally:
        conn.close()


def load_fat_scores() -> Dict[int, Dict[str, Any]]:
    """Load fat_reason_tags from catfood_fat_material_features_scored."""
    conn = _connect(FEATURE_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    formula_id,
                    fat_reason_tags
                FROM {_quote('catfood_fat_material_features_scored')}
                WHERE formula_id IS NOT NULL
            """)
            rows = cur.fetchall()
        return {int(r["formula_id"]): r for r in rows}
    finally:
        conn.close()


# =========================
# Table creation & upsert
# =========================

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_quote(OUTPUT_TABLE)} (
    formula_id                  BIGINT UNSIGNED NOT NULL,
    product_id                  BIGINT UNSIGNED NOT NULL,
    brand_id                    BIGINT NOT NULL,
    standard_brand_name         VARCHAR(255) NULL,
    standard_product_name       VARCHAR(512) NULL,
    normalized_ingredients_json JSON NULL,
    ingredient_composition      LONGTEXT NULL,
    nutrition_json              JSON NULL,
    protein_score               DECIMAL(10,4) NULL,
    carb_score                  DECIMAL(10,4) NULL,
    fiber_score                 DECIMAL(10,4) NULL,
    fat_score                   DECIMAL(10,4) NULL,
    prebiotic_score             DECIMAL(10,4) NULL,
    antioxidant_score           DECIMAL(10,4) NULL,
    p_buffer                    DECIMAL(10,4) NULL,
    q_feed                      DECIMAL(10,4) NULL,
    q_scfa                      DECIMAL(10,4) NULL,
    p_level                     VARCHAR(16) NULL,
    q_level                     VARCHAR(16) NULL,
    fat_reason_tags             TEXT NULL,
    life_stage                  VARCHAR(64) NULL,
    product_type                VARCHAR(64) NULL,
    created_at                  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (formula_id),
    KEY idx_product_id (product_id),
    KEY idx_brand_id (brand_id),
    KEY idx_brand_product (standard_brand_name(100), standard_product_name(100))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

UPSERT_SQL = f"""
INSERT INTO {_quote(OUTPUT_TABLE)} (
    formula_id, product_id, brand_id,
    standard_brand_name, standard_product_name,
    normalized_ingredients_json, ingredient_composition, nutrition_json,
    protein_score, carb_score, fiber_score, fat_score,
    prebiotic_score, antioxidant_score,
    p_buffer, q_feed, q_scfa,
    p_level, q_level,
    fat_reason_tags,
    life_stage, product_type,
    created_at, updated_at
) VALUES (
    %s, %s, %s,
    %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s,
    %s, %s, %s,
    %s, %s,
    %s,
    %s, %s,
    NOW(), NOW()
)
ON DUPLICATE KEY UPDATE
    product_id                  = VALUES(product_id),
    brand_id                    = VALUES(brand_id),
    standard_brand_name         = VALUES(standard_brand_name),
    standard_product_name       = VALUES(standard_product_name),
    normalized_ingredients_json = VALUES(normalized_ingredients_json),
    ingredient_composition      = VALUES(ingredient_composition),
    nutrition_json              = VALUES(nutrition_json),
    protein_score               = VALUES(protein_score),
    carb_score                  = VALUES(carb_score),
    fiber_score                 = VALUES(fiber_score),
    fat_score                   = VALUES(fat_score),
    prebiotic_score             = VALUES(prebiotic_score),
    antioxidant_score           = VALUES(antioxidant_score),
    p_buffer                    = VALUES(p_buffer),
    q_feed                      = VALUES(q_feed),
    q_scfa                      = VALUES(q_scfa),
    p_level                     = VALUES(p_level),
    q_level                     = VALUES(q_level),
    fat_reason_tags             = VALUES(fat_reason_tags),
    life_stage                  = VALUES(life_stage),
    product_type                = VALUES(product_type),
    updated_at                  = NOW()
"""


# =========================
# Main
# =========================

def main() -> None:
    # Load all data sources
    print("加载标准配方基础数据 ...")
    formulas = load_formula_base()
    print(f"  catfood_standard_formula: {len(formulas)} 行")

    print("加载评分数据 ...")
    feat_scores = load_feature_scores()
    print(f"  sku_feature_input: {len(feat_scores)} 个 formula_id")

    fiber_scores = load_fiber_scores()
    print(f"  catfood_fiber_feature_score: {len(fiber_scores)} 个 formula_id")

    fat_scores = load_fat_scores()
    print(f"  catfood_fat_material_features_scored: {len(fat_scores)} 个 formula_id")

    # Create output table
    conn = _connect(FEATURE_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {_quote(OUTPUT_TABLE)}")
            cur.execute(CREATE_TABLE_SQL)

        # Build & upsert each formula row
        stats = {"total": 0, "with_scores": 0, "with_fiber": 0, "with_fat": 0}

        for row in formulas:
            fid = int(row["formula_id"])
            feat = feat_scores.get(fid, {})
            fiber = fiber_scores.get(fid, {})
            fat = fat_scores.get(fid, {})

            normalized_ingredients = _to_json(row.get("normalized_ingredients_json"))
            nutrition = _to_json(row.get("nutrition_json"))

            params = (
                fid,
                int(row["product_id"]),
                int(row["brand_id"]),
                row.get("standard_brand_name"),
                row.get("standard_product_name"),
                normalized_ingredients,
                row.get("raw_ingredient_example"),
                nutrition,
                _to_decimal(feat.get("protein_score")),
                _to_decimal(feat.get("carb_score")),
                _to_decimal(feat.get("fiber_score")),
                _to_decimal(feat.get("fat_score")),
                _to_decimal(feat.get("prebiotic_score")),
                _to_decimal(feat.get("antioxidant_score")),
                _to_decimal(feat.get("p_buffer")),
                _to_decimal(feat.get("q_feed")),
                _to_decimal(feat.get("q_scfa")),
                fiber.get("p_level"),
                fiber.get("q_level"),
                fat.get("fat_reason_tags"),
                row.get("life_stage"),
                row.get("product_type"),
            )

            with conn.cursor() as cur:
                cur.execute(UPSERT_SQL, params)

            stats["total"] += 1
            if feat:
                stats["with_scores"] += 1
            if fiber:
                stats["with_fiber"] += 1
            if fat:
                stats["with_fat"] += 1

        conn.commit()

        # Verify
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {_quote(OUTPUT_TABLE)}")
            final_count = cur.fetchone()["cnt"]

        print(f"\n构建完成！")
        print(f"  目标表: {FEATURE_DB}.{OUTPUT_TABLE}")
        print(f"  总行数: {final_count}")
        print(f"  有评分数据: {stats['with_scores']}/{stats['total']}")
        print(f"  有纤维评分: {stats['with_fiber']}/{stats['total']}")
        print(f"  有油脂评分: {stats['with_fat']}/{stats['total']}")

        # Show column count
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) AS col_cnt
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """, (FEATURE_DB, OUTPUT_TABLE))
            col_cnt = cur.fetchone()["col_cnt"]
        print(f"  字段数: {col_cnt}")

        # Preview
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT formula_id, standard_brand_name, standard_product_name,
                       protein_score, carb_score, fiber_score, fat_score,
                       JSON_LENGTH(normalized_ingredients_json) AS ingredient_count
                FROM {_quote(OUTPUT_TABLE)}
                ORDER BY standard_brand_name, standard_product_name
                LIMIT 10
            """)
            print(f"\n前 10 行预览:")
            for r in cur.fetchall():
                print(f"  fid={r['formula_id']}, {r['standard_brand_name']} / {r['standard_product_name']}, "
                      f"p={r['protein_score']}, c={r['carb_score']}, f={r['fiber_score']}, "
                      f"fat={r['fat_score']}, ingredients={r['ingredient_count']}")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
