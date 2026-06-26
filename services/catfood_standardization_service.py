"""Immutable OCR-to-brand/product/formula standardization pipeline."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql
import yaml

from app_config import get_mysql_config


BASE_DIR = Path(__file__).resolve().parents[1]
BRAND_MASTER_PATH = (
    BASE_DIR / "vendor" / "csv_mysql_labeling" / "config" / "catfood_brand_master.yaml"
)

BRAND_TABLE = "catfood_standard_brand"
BRAND_ALIAS_TABLE = "catfood_standard_brand_alias"
PRODUCT_TABLE = "catfood_standard_product"
PRODUCT_ALIAS_TABLE = "catfood_standard_product_alias"
FORMULA_TABLE = "catfood_standard_formula"
FORMULA_INPUT_TABLE = "catfood_formula_feature_input"
MAPPING_TABLE = "catfood_ocr_standard_mapping"
BRAND_CANDIDATE_TABLE = "catfood_standard_brand_candidate"
PRODUCT_CANDIDATE_TABLE = "catfood_standard_product_candidate"


def _connect():
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "nan" else re.sub(r"\s+", " ", text)


def normalize_name(value: Any) -> str:
    return re.sub(
        r"[\s·•._\-—–/\\|,:：;；，。()（）\[\]【】'\"®™]+",
        "",
        _clean(value).lower(),
    )


def init_standardization_db() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{BRAND_TABLE}` (
                  brand_id BIGINT NOT NULL,
                  standard_brand_name VARCHAR(255) NOT NULL,
                  origin_type VARCHAR(64) NULL,
                  origin_country VARCHAR(128) NULL,
                  brand_tier VARCHAR(64) NULL,
                  min_price_per_jin DECIMAL(10,2) NULL,
                  max_price_per_jin DECIMAL(10,2) NULL,
                  price_band VARCHAR(64) NULL,
                  image_url VARCHAR(1024) NULL,
                  active TINYINT NOT NULL DEFAULT 1,
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  PRIMARY KEY (brand_id),
                  UNIQUE KEY uq_standard_brand_name (standard_brand_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(f"SHOW COLUMNS FROM `{BRAND_TABLE}`")
            brand_columns = {row["Field"] for row in cursor.fetchall()}
            for column_name, definition in (
                ("min_price_per_jin", "DECIMAL(10,2) NULL AFTER brand_tier"),
                ("max_price_per_jin", "DECIMAL(10,2) NULL AFTER min_price_per_jin"),
                ("price_band", "VARCHAR(64) NULL AFTER max_price_per_jin"),
            ):
                if column_name not in brand_columns:
                    cursor.execute(
                        f"ALTER TABLE `{BRAND_TABLE}` ADD COLUMN `{column_name}` {definition}"
                    )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{BRAND_ALIAS_TABLE}` (
                  alias_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  brand_id BIGINT NOT NULL,
                  alias_name VARCHAR(255) NOT NULL,
                  normalized_alias VARCHAR(255) NOT NULL,
                  source VARCHAR(64) NULL,
                  active TINYINT NOT NULL DEFAULT 1,
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (alias_id),
                  UNIQUE KEY uq_brand_alias_normalized (normalized_alias),
                  KEY idx_brand_alias_brand_id (brand_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{PRODUCT_TABLE}` (
                  product_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  brand_id BIGINT NOT NULL,
                  standard_product_name VARCHAR(512) NOT NULL,
                  display_name VARCHAR(255) NOT NULL,
                  display_subtitle VARCHAR(255) NULL,
                  candidate_type VARCHAR(32) NULL,
                  product_country VARCHAR(128) NULL,
                  price_band VARCHAR(64) NULL,
                  product_type VARCHAR(64) NULL,
                  life_stage VARCHAR(64) NULL,
                  image_url VARCHAR(1024) NULL,
                  active TINYINT NOT NULL DEFAULT 1,
                  source_candidate_id BIGINT NULL,
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  PRIMARY KEY (product_id),
                  UNIQUE KEY uq_brand_standard_product (brand_id, standard_product_name),
                  KEY idx_product_brand_id (brand_id),
                  KEY idx_product_display_name (display_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{PRODUCT_ALIAS_TABLE}` (
                  alias_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  product_id BIGINT UNSIGNED NOT NULL,
                  brand_id BIGINT NOT NULL,
                  alias_name VARCHAR(512) NOT NULL,
                  normalized_alias VARCHAR(512) NOT NULL,
                  source VARCHAR(64) NULL,
                  active TINYINT NOT NULL DEFAULT 1,
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (alias_id),
                  UNIQUE KEY uq_product_alias (brand_id, normalized_alias),
                  KEY idx_product_alias_product_id (product_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{FORMULA_TABLE}` (
                  formula_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  product_id BIGINT UNSIGNED NOT NULL,
                  formula_version INT NOT NULL DEFAULT 1,
                  raw_ingredient_example LONGTEXT NULL,
                  normalized_ingredient_composition LONGTEXT NOT NULL,
                  normalized_ingredients_json JSON NULL,
                  ingredient_fingerprint CHAR(64) NOT NULL,
                  nutrition_json JSON NULL,
                  effective_from DATE NULL,
                  effective_to DATE NULL,
                  is_current TINYINT NOT NULL DEFAULT 1,
                  status VARCHAR(32) NOT NULL DEFAULT 'active',
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  PRIMARY KEY (formula_id),
                  UNIQUE KEY uq_product_formula_fingerprint (product_id, ingredient_fingerprint),
                  KEY idx_formula_product_current (product_id, is_current)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{FORMULA_INPUT_TABLE}` (
                  formula_id BIGINT UNSIGNED NOT NULL,
                  product_id BIGINT UNSIGNED NOT NULL,
                  brand_id BIGINT NOT NULL,
                  formula_version INT NOT NULL,
                  is_current TINYINT NOT NULL DEFAULT 1,
                  id BIGINT UNSIGNED NOT NULL,
                  source_id BIGINT NULL,
                  parsed_row_id BIGINT NULL,
                  canonical_source_id BIGINT NULL,
                  source_ids_json JSON NULL,
                  merged_source_ids TEXT NULL,
                  brand VARCHAR(255) NOT NULL,
                  product_name VARCHAR(512) NOT NULL,
                  image_name VARCHAR(255) NULL,
                  image_path VARCHAR(1024) NULL,
                  file_sha256 CHAR(64) NULL,
                  ingredient_composition LONGTEXT NOT NULL,
                  normalized_ingredient_composition LONGTEXT NOT NULL,
                  normalized_ingredients_json JSON NULL,
                  ingredient_fingerprint CHAR(64) NOT NULL,
                  nutrition_json JSON NULL,
                  nutrition_completeness DECIMAL(6,5) NOT NULL DEFAULT 0,
                  input_hash CHAR(64) NOT NULL,
                  input_version VARCHAR(32) NOT NULL DEFAULT 'v1',
                  build_status VARCHAR(32) NOT NULL,
                  parse_batch_id VARCHAR(32) NOT NULL DEFAULT 'formula_input',
                  parse_ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (formula_id),
                  UNIQUE KEY uq_formula_input_id (id),
                  KEY idx_formula_input_source_id (source_id),
                  KEY idx_formula_input_product_current (product_id, is_current),
                  KEY idx_formula_input_status (build_status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{BRAND_CANDIDATE_TABLE}` (
                  candidate_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  raw_brand_name VARCHAR(255) NOT NULL,
                  normalized_brand_name VARCHAR(255) NOT NULL,
                  occurrence_count INT NOT NULL DEFAULT 1,
                  source_product_candidate_ids_json JSON NULL,
                  suggested_brand_id BIGINT NULL,
                  suggested_standard_brand_name VARCHAR(255) NULL,
                  status VARCHAR(32) NOT NULL DEFAULT 'pending',
                  reviewer VARCHAR(128) NULL,
                  review_note TEXT NULL,
                  reviewed_at DATETIME NULL,
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  PRIMARY KEY (candidate_id),
                  UNIQUE KEY uq_brand_candidate_normalized (normalized_brand_name),
                  KEY idx_brand_candidate_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{PRODUCT_CANDIDATE_TABLE}` (
                  product_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  brand_id BIGINT NULL,
                  standard_brand_name VARCHAR(255) NULL,
                  standard_product_name VARCHAR(512) NOT NULL,
                  display_name VARCHAR(255) NULL,
                  display_name_rule VARCHAR(32) NOT NULL,
                  display_subtitle VARCHAR(255) NULL,
                  candidate_type VARCHAR(32) NOT NULL DEFAULT 'unknown',
                  model_name VARCHAR(64) NULL,
                  series_name VARCHAR(128) NULL,
                  function_name VARCHAR(128) NULL,
                  process_name VARCHAR(64) NULL,
                  flavor_or_protein VARCHAR(128) NULL,
                  product_country VARCHAR(128) NULL,
                  price_band VARCHAR(64) NULL,
                  product_type VARCHAR(64) NULL,
                  life_stage VARCHAR(64) NULL,
                  product_image VARCHAR(1024) NULL,
                  active TINYINT NOT NULL DEFAULT 0,
                  quality_level VARCHAR(16) NOT NULL,
                  review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                  normalized_tags_json JSON NULL,
                  truncation_suspected TINYINT NOT NULL DEFAULT 0,
                  reject_reason VARCHAR(512) NULL,
                  model_reason TEXT NULL,
                  hard_filter_reason VARCHAR(128) NULL,
                  model_name_used VARCHAR(128) NULL,
                  model_prompt_version VARCHAR(64) NULL,
                  model_raw_result_json JSON NULL,
                  quality_reasons_json JSON NULL,
                  source_ids_json JSON NOT NULL,
                  parsed_row_ids_json JSON NOT NULL,
                  raw_product_names_json JSON NULL,
                  evidence_json JSON NULL,
                  build_batch_id CHAR(32) NOT NULL,
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  PRIMARY KEY (product_id),
                  KEY idx_brand_id (brand_id),
                  KEY idx_display_name (display_name),
                  KEY idx_quality_review (quality_level, review_status),
                  UNIQUE KEY uq_brand_product (brand_id, standard_product_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{MAPPING_TABLE}` (
                  mapping_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  source_id BIGINT NOT NULL,
                  parsed_row_id BIGINT NULL,
                  image_id VARCHAR(64) NULL,
                  file_sha256 CHAR(64) NULL,
                  raw_brand_name VARCHAR(255) NULL,
                  raw_product_name VARCHAR(512) NULL,
                  raw_ingredient_composition LONGTEXT NULL,
                  brand_id BIGINT NULL,
                  product_id BIGINT UNSIGNED NULL,
                  formula_id BIGINT UNSIGNED NULL,
                  brand_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                  product_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                  formula_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                  overall_status VARCHAR(32) NOT NULL DEFAULT 'pending',
                  brand_confidence DECIMAL(6,5) NULL,
                  product_confidence DECIMAL(6,5) NULL,
                  formula_confidence DECIMAL(6,5) NULL,
                  match_evidence_json JSON NULL,
                  reviewer VARCHAR(128) NULL,
                  review_note TEXT NULL,
                  reviewed_at DATETIME NULL,
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                  PRIMARY KEY (mapping_id),
                  UNIQUE KEY uq_mapping_source_id (source_id),
                  KEY idx_mapping_brand_product_formula (brand_id, product_id, formula_id),
                  KEY idx_mapping_overall_status (overall_status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()
    seed_brand_master()


def _nutrition_completeness(nutrition: dict[str, Any]) -> float:
    required_metrics = {"粗蛋白", "粗脂肪", "粗纤维", "水分", "粗灰分", "钙", "总磷"}
    present = {
        _clean(item.get("metric_name"))
        for item in nutrition.values()
        if isinstance(item, dict) and _clean(item.get("metric_name"))
    }
    return round(len(required_metrics & present) / len(required_metrics), 5)


def _canonical_formula_source(
    cursor,
    *,
    formula_id: int,
) -> tuple[dict[str, Any] | None, list[int]]:
    cursor.execute(
        f"""
        SELECT
          m.source_id,
          m.parsed_row_id,
          p.image_name,
          p.image_path,
          p.file_sha256,
          p.parse_ts,
          p.updated_ts
        FROM `{MAPPING_TABLE}` m
        LEFT JOIN catfood_ingredient_ocr_parsed p
          ON p.source_id = m.source_id
        WHERE m.formula_id = %s
        ORDER BY
          (p.ingredient_composition IS NOT NULL AND TRIM(p.ingredient_composition) <> '') DESC,
          p.updated_ts DESC,
          p.id DESC,
          m.mapping_id DESC
        """,
        (formula_id,),
    )
    rows = list(cursor.fetchall())
    source_ids = list(
        dict.fromkeys(int(row["source_id"]) for row in rows if row.get("source_id") is not None)
    )
    return (rows[0] if rows else None), source_ids


def build_formula_feature_input(
    *,
    formula_id: int,
    apply: bool = True,
    initialize: bool = True,
) -> dict[str, Any]:
    """Build one stable, formula-keyed input row for downstream feature models."""
    if initialize:
        init_standardization_db()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                  f.*,
                  p.brand_id,
                  p.standard_product_name,
                  b.standard_brand_name
                FROM `{FORMULA_TABLE}` f
                JOIN `{PRODUCT_TABLE}` p ON p.product_id = f.product_id
                JOIN `{BRAND_TABLE}` b ON b.brand_id = p.brand_id
                WHERE f.formula_id = %s
                  AND f.status = 'active'
                """,
                (int(formula_id),),
            )
            formula = cursor.fetchone()
            if not formula:
                raise KeyError(f"formula_id 不存在或未启用: {formula_id}")

            source, source_ids = _canonical_formula_source(
                cursor,
                formula_id=int(formula_id),
            )
            nutrition = _json_dict(formula.get("nutrition_json"))
            if not nutrition and source and source.get("source_id") is not None:
                nutrition = _load_nutrition_signature(cursor, int(source["source_id"]))
            raw_ingredients = _clean(formula.get("raw_ingredient_example"))
            normalized = _clean(formula.get("normalized_ingredient_composition"))
            normalized_items = _json_list(formula.get("normalized_ingredients_json"))
            if not raw_ingredients:
                raw_ingredients = normalized
            completeness = _nutrition_completeness(nutrition)
            build_status = (
                "blocked"
                if not normalized_items
                else "ready"
                if completeness >= 0.7
                else "nutrition_missing"
            )
            input_payload = {
                "formula_id": int(formula["formula_id"]),
                "product_id": int(formula["product_id"]),
                "brand_id": int(formula["brand_id"]),
                "formula_version": int(formula["formula_version"]),
                "brand": formula["standard_brand_name"],
                "product_name": formula["standard_product_name"],
                "raw_ingredient_composition": raw_ingredients,
                "normalized_ingredients": normalized_items,
                "nutrition": nutrition,
                "source_ids": source_ids,
            }
            input_hash = hashlib.sha256(
                json.dumps(
                    input_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            existing_hash = None
            cursor.execute(
                f"SELECT input_hash FROM `{FORMULA_INPUT_TABLE}` WHERE formula_id = %s",
                (int(formula_id),),
            )
            existing = cursor.fetchone()
            if existing:
                existing_hash = existing.get("input_hash")

            if apply:
                canonical_source_id = (
                    int(source["source_id"])
                    if source and source.get("source_id") is not None
                    else None
                )
                compatibility_source_id = canonical_source_id or -(10**15 + int(formula_id))
                synthetic_id = int(formula_id)
                if int(formula["is_current"]):
                    cursor.execute(
                        f"""
                        UPDATE `{FORMULA_INPUT_TABLE}`
                        SET is_current = 0
                        WHERE product_id = %s AND formula_id <> %s
                        """,
                        (formula["product_id"], formula["formula_id"]),
                    )
                cursor.execute(
                    f"""
                    INSERT INTO `{FORMULA_INPUT_TABLE}`(
                      formula_id, product_id, brand_id, formula_version, is_current,
                      id, source_id, parsed_row_id, canonical_source_id,
                      source_ids_json, merged_source_ids,
                      brand, product_name, image_name, image_path, file_sha256,
                      ingredient_composition, normalized_ingredient_composition,
                      normalized_ingredients_json, ingredient_fingerprint,
                      nutrition_json, nutrition_completeness, input_hash, build_status,
                      parse_batch_id, parse_ts, updated_ts
                    ) VALUES(
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s,
                      %s, %s,
                      %s, %s, %s, %s,
                      'formula_input', NOW(), NOW()
                    )
                    ON DUPLICATE KEY UPDATE
                      product_id = VALUES(product_id),
                      brand_id = VALUES(brand_id),
                      formula_version = VALUES(formula_version),
                      is_current = VALUES(is_current),
                      source_id = VALUES(source_id),
                      parsed_row_id = VALUES(parsed_row_id),
                      canonical_source_id = VALUES(canonical_source_id),
                      source_ids_json = VALUES(source_ids_json),
                      merged_source_ids = VALUES(merged_source_ids),
                      brand = VALUES(brand),
                      product_name = VALUES(product_name),
                      image_name = VALUES(image_name),
                      image_path = VALUES(image_path),
                      file_sha256 = VALUES(file_sha256),
                      ingredient_composition = VALUES(ingredient_composition),
                      normalized_ingredient_composition = VALUES(normalized_ingredient_composition),
                      normalized_ingredients_json = VALUES(normalized_ingredients_json),
                      ingredient_fingerprint = VALUES(ingredient_fingerprint),
                      nutrition_json = VALUES(nutrition_json),
                      nutrition_completeness = VALUES(nutrition_completeness),
                      build_status = VALUES(build_status),
                      updated_ts = IF(input_hash <> VALUES(input_hash), NOW(), updated_ts),
                      input_hash = VALUES(input_hash)
                    """,
                    (
                        formula["formula_id"],
                        formula["product_id"],
                        formula["brand_id"],
                        formula["formula_version"],
                        formula["is_current"],
                        synthetic_id,
                        compatibility_source_id,
                        source.get("parsed_row_id") if source else None,
                        canonical_source_id,
                        json.dumps(source_ids, ensure_ascii=False),
                        ",".join(str(value) for value in source_ids) or None,
                        formula["standard_brand_name"],
                        formula["standard_product_name"],
                        source.get("image_name") if source else None,
                        source.get("image_path") if source else None,
                        source.get("file_sha256") if source else None,
                        raw_ingredients,
                        normalized,
                        json.dumps(normalized_items, ensure_ascii=False),
                        formula["ingredient_fingerprint"],
                        json.dumps(nutrition, ensure_ascii=False),
                        completeness,
                        input_hash,
                        build_status,
                    ),
                )
            if apply:
                conn.commit()
            else:
                conn.rollback()
    result = {
        "ok": True,
        "formula_id": int(formula_id),
        "product_id": int(formula["product_id"]),
        "brand_id": int(formula["brand_id"]),
        "source_id": (
            source.get("source_id")
            if source and source.get("source_id") is not None
            else -(10**15 + int(formula_id))
        ),
        "source_ids": source_ids,
        "input_hash": input_hash,
        "changed": existing_hash != input_hash,
        "build_status": build_status,
        "nutrition_completeness": completeness,
    }
    return result


def rebuild_formula_feature_inputs(*, apply: bool = True) -> dict[str, Any]:
    init_standardization_db()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT formula_id
                FROM `{FORMULA_TABLE}`
                WHERE status = 'active'
                ORDER BY formula_id
                """
            )
            formula_ids = [int(row["formula_id"]) for row in cursor.fetchall()]
    items = [
        build_formula_feature_input(
            formula_id=formula_id,
            apply=apply,
            initialize=False,
        )
        for formula_id in formula_ids
    ]
    result = {
        "ok": True,
        "applied": apply,
        "count": len(items),
        "changed": sum(bool(item["changed"]) for item in items),
        "status_counts": {
            status: sum(item["build_status"] == status for item in items)
            for status in ("ready", "nutrition_missing", "blocked")
        },
        "items": items,
    }
    return result


def initialize_brand_candidates() -> dict[str, int]:
    """Collect product-candidate brand names that cannot resolve to the brand master."""
    init_standardization_db()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW TABLES LIKE 'catfood_standard_product_candidate'")
            if not cursor.fetchone():
                return {"scanned": 0, "upserted": 0}
            cursor.execute(
                """
                SELECT product_id, standard_brand_name
                FROM catfood_standard_product_candidate
                WHERE standard_brand_name IS NOT NULL
                  AND TRIM(standard_brand_name) <> ''
                """
            )
            rows = cursor.fetchall()

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_name = _clean(row.get("standard_brand_name"))
        normalized = normalize_name(raw_name)
        if not normalized or resolve_standard_brand(raw_name):
            continue
        group = grouped.setdefault(
            normalized,
            {"raw_brand_name": raw_name, "product_ids": []},
        )
        group["product_ids"].append(int(row["product_id"]))

    with _connect() as conn:
        with conn.cursor() as cursor:
            for normalized, group in grouped.items():
                cursor.execute(
                    f"""
                    INSERT INTO `{BRAND_CANDIDATE_TABLE}`(
                      raw_brand_name, normalized_brand_name, occurrence_count,
                      source_product_candidate_ids_json, status
                    ) VALUES(%s, %s, %s, %s, 'pending')
                    ON DUPLICATE KEY UPDATE
                      raw_brand_name = VALUES(raw_brand_name),
                      occurrence_count = VALUES(occurrence_count),
                      source_product_candidate_ids_json = VALUES(source_product_candidate_ids_json),
                      status = CASE WHEN status = 'rejected' THEN status ELSE 'pending' END,
                      updated_at = NOW()
                    """,
                    (
                        group["raw_brand_name"],
                        normalized,
                        len(group["product_ids"]),
                        json.dumps(sorted(group["product_ids"]), ensure_ascii=False),
                    ),
                )
        conn.commit()
    return {"scanned": len(rows), "upserted": len(grouped)}


def list_brand_candidates(*, status: str = "pending", limit: int = 200) -> dict[str, Any]:
    init_standardization_db()
    limit = max(1, min(int(limit or 200), 500))
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE status = %s"
        params.append(status)
    params.append(limit)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM `{BRAND_CANDIDATE_TABLE}`
                {where}
                ORDER BY occurrence_count DESC, candidate_id
                LIMIT %s
                """,
                params,
            )
            items = cursor.fetchall()
            cursor.execute(
                f"""
                SELECT status, COUNT(*) AS count
                FROM `{BRAND_CANDIDATE_TABLE}`
                GROUP BY status
                """
            )
            counts = {row["status"]: int(row["count"]) for row in cursor.fetchall()}
    for item in items:
        item["source_product_candidate_ids"] = _json_list(
            item.pop("source_product_candidate_ids_json", None)
        )
    return {"ok": True, "items": items, "count": len(items), "status_counts": counts}


def review_brand_candidate(
    candidate_id: int,
    payload: dict[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    """Map a candidate to an existing brand or create a brand in the brand workflow."""
    init_standardization_db()
    action = _clean(action)
    if action not in {"map", "create", "reject"}:
        raise ValueError("action 必须是 map、create 或 reject")
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM `{BRAND_CANDIDATE_TABLE}` WHERE candidate_id = %s FOR UPDATE",
                (int(candidate_id),),
            )
            candidate = cursor.fetchone()
            if not candidate:
                raise KeyError(f"brand candidate 不存在: {candidate_id}")
            if action == "reject":
                cursor.execute(
                    f"""
                    UPDATE `{BRAND_CANDIDATE_TABLE}`
                    SET status = 'rejected', reviewer = %s, review_note = %s,
                        reviewed_at = NOW()
                    WHERE candidate_id = %s
                    """,
                    (
                        _clean(payload.get("reviewer")) or None,
                        _clean(payload.get("review_note")) or None,
                        int(candidate_id),
                    ),
                )
                conn.commit()
                return {"ok": True, "candidate_id": candidate_id, "status": "rejected"}

            requested_name = _clean(
                payload.get("standard_brand_name") or candidate["raw_brand_name"]
            )
            if action == "map":
                brand = resolve_standard_brand(requested_name)
                if not brand:
                    raise ValueError("选择的标准品牌不存在")
            else:
                if resolve_standard_brand(requested_name):
                    raise ValueError("品牌已存在，请使用映射操作")
                cursor.execute(
                    f"SELECT COALESCE(MAX(brand_id), 0) + 1 AS next_id FROM `{BRAND_TABLE}`"
                )
                brand_id = int(cursor.fetchone()["next_id"])
                origin_type = _clean(payload.get("origin_type"))
                if origin_type not in {"国产品牌", "进口/国际品牌"}:
                    raise ValueError("创建品牌时必须选择国产品牌或进口/国际品牌")
                cursor.execute(
                    f"""
                    INSERT INTO `{BRAND_TABLE}`(
                      brand_id, standard_brand_name, origin_type, active
                    ) VALUES(%s, %s, %s, 1)
                    """,
                    (brand_id, requested_name, origin_type),
                )
                cursor.execute(
                    f"""
                    INSERT INTO `{BRAND_ALIAS_TABLE}`(
                      brand_id, alias_name, normalized_alias, source, active
                    ) VALUES(%s, %s, %s, 'brand_candidate_review', 1)
                    """,
                    (brand_id, requested_name, normalize_name(requested_name)),
                )
                brand = {"brand_id": brand_id, "standard_brand_name": requested_name}

            raw_name = candidate["raw_brand_name"]
            raw_normalized = normalize_name(raw_name)
            cursor.execute(
                f"""
                INSERT INTO `{BRAND_ALIAS_TABLE}`(
                  brand_id, alias_name, normalized_alias, source, active
                ) VALUES(%s, %s, %s, 'brand_candidate_review', 1)
                ON DUPLICATE KEY UPDATE
                  brand_id = VALUES(brand_id), alias_name = VALUES(alias_name), active = 1
                """,
                (brand["brand_id"], raw_name, raw_normalized),
            )
            cursor.execute(
                f"""
                UPDATE `{BRAND_CANDIDATE_TABLE}`
                SET suggested_brand_id = %s,
                    suggested_standard_brand_name = %s,
                    status = 'approved',
                    reviewer = %s,
                    review_note = %s,
                    reviewed_at = NOW()
                WHERE candidate_id = %s
                """,
                (
                    brand["brand_id"],
                    brand["standard_brand_name"],
                    _clean(payload.get("reviewer")) or None,
                    _clean(payload.get("review_note")) or None,
                    int(candidate_id),
                ),
            )
            cursor.execute(
                """
                SELECT *
                FROM catfood_standard_product_candidate
                WHERE standard_brand_name = %s
                   OR REPLACE(LOWER(standard_brand_name), ' ', '') = %s
                ORDER BY product_id
                FOR UPDATE
                """,
                (raw_name, raw_normalized),
            )
            product_candidates = cursor.fetchall()
            merged_product_candidates = 0
            for product_candidate in product_candidates:
                merged_product_candidates += _assign_candidate_brand(
                    cursor,
                    product_candidate,
                    brand_id=int(brand["brand_id"]),
                    standard_brand_name=brand["standard_brand_name"],
                )
            cursor.execute(
                f"""
                UPDATE `{MAPPING_TABLE}`
                SET brand_id = %s,
                    brand_status = 'matched',
                    brand_confidence = 1.0,
                    overall_status = 'pending'
                WHERE raw_brand_name = %s
                """,
                (brand["brand_id"], raw_name),
            )
        conn.commit()
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "status": "approved",
        "brand_id": brand["brand_id"],
        "standard_brand_name": brand["standard_brand_name"],
        "merged_product_candidates": merged_product_candidates,
    }


def _assign_candidate_brand(
    cursor,
    product_candidate: dict[str, Any],
    *,
    brand_id: int,
    standard_brand_name: str,
) -> int:
    """Assign a brand and collapse a duplicate candidate without losing OCR lineage."""
    product_id = int(product_candidate["product_id"])
    cursor.execute(
        """
        SELECT *
        FROM catfood_standard_product_candidate
        WHERE brand_id = %s
          AND standard_product_name = %s
          AND product_id <> %s
        ORDER BY
          CASE review_status
            WHEN 'approved' THEN 0
            WHEN 'needs_manual_review' THEN 1
            WHEN 'pending' THEN 2
            ELSE 3
          END,
          active DESC,
          product_id
        LIMIT 1
        FOR UPDATE
        """,
        (brand_id, product_candidate["standard_product_name"], product_id),
    )
    existing = cursor.fetchone()
    if not existing:
        cursor.execute(
            """
            UPDATE catfood_standard_product_candidate
            SET brand_id = %s,
                standard_brand_name = %s,
                updated_at = NOW()
            WHERE product_id = %s
            """,
            (brand_id, standard_brand_name, product_id),
        )
        return 0

    list_columns = (
        "source_ids_json",
        "parsed_row_ids_json",
        "raw_product_names_json",
        "normalized_tags_json",
        "quality_reasons_json",
    )
    merged_lists = {
        column: json.dumps(
            _merge_json_lists(existing.get(column), product_candidate.get(column)),
            ensure_ascii=False,
        )
        for column in list_columns
    }
    evidence = _json_dict(existing.get("evidence_json"))
    merged_ids = _merge_json_lists(evidence.get("merged_candidate_ids"), [product_id])
    evidence["merged_candidate_ids"] = merged_ids

    review_status = _preferred_value(
        existing.get("review_status"),
        product_candidate.get("review_status"),
        ("approved", "needs_manual_review", "pending", "rejected"),
    )
    quality_level = _preferred_value(
        existing.get("quality_level"),
        product_candidate.get("quality_level"),
        ("strong", "medium", "weak", "invalid"),
    )
    cursor.execute(
        """
        UPDATE catfood_standard_product_candidate
        SET standard_brand_name = %s,
            active = GREATEST(active, %s),
            review_status = %s,
            quality_level = %s,
            source_ids_json = %s,
            parsed_row_ids_json = %s,
            raw_product_names_json = %s,
            normalized_tags_json = %s,
            quality_reasons_json = %s,
            evidence_json = %s,
            updated_at = NOW()
        WHERE product_id = %s
        """,
        (
            standard_brand_name,
            int(product_candidate.get("active") or 0),
            review_status,
            quality_level,
            merged_lists["source_ids_json"],
            merged_lists["parsed_row_ids_json"],
            merged_lists["raw_product_names_json"],
            merged_lists["normalized_tags_json"],
            merged_lists["quality_reasons_json"],
            json.dumps(evidence, ensure_ascii=False),
            int(existing["product_id"]),
        ),
    )
    cursor.execute(
        "DELETE FROM catfood_standard_product_candidate WHERE product_id = %s",
        (product_id,),
    )
    return 1


def _merge_json_lists(*values: Any) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for value in values:
        items = _json_list(value)
        for item in items:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if marker not in seen:
                seen.add(marker)
                merged.append(item)
    return merged


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _preferred_value(left: Any, right: Any, order: tuple[str, ...]) -> str:
    left_text = _clean(left)
    right_text = _clean(right)
    rank = {value: index for index, value in enumerate(order)}
    return min((left_text, right_text), key=lambda value: rank.get(value, len(rank)))


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _merge_product_candidate_lineage(
    existing: dict[str, Any] | None,
    *,
    source_id: int,
    parsed_row_id: int | None,
    raw_product_name: str,
    image_id: str = "",
) -> dict[str, Any]:
    existing = existing or {}
    evidence = _json_dict(existing.get("evidence_json"))
    evidence.update(
        {
            "origin": "catfood_image_analysis",
            "latest_source_id": source_id,
        }
    )
    if image_id:
        evidence["image_id"] = image_id
    current_status = _clean(existing.get("review_status"))
    review_status = (
        current_status
        if current_status in {"approved", "rejected"}
        else "needs_manual_review"
    )
    return {
        "review_status": review_status,
        "source_ids_json": json.dumps(
            _merge_json_lists(existing.get("source_ids_json"), [source_id]),
            ensure_ascii=False,
        ),
        "parsed_row_ids_json": json.dumps(
            _merge_json_lists(
                existing.get("parsed_row_ids_json"),
                [parsed_row_id] if parsed_row_id is not None else [],
            ),
            ensure_ascii=False,
        ),
        "raw_product_names_json": json.dumps(
            _merge_json_lists(existing.get("raw_product_names_json"), [raw_product_name]),
            ensure_ascii=False,
        ),
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
    }


def _upsert_product_review_candidate(
    cursor,
    *,
    mapping: dict[str, Any],
    product_status: str,
) -> int | None:
    raw_product_name = _clean(mapping.get("raw_product_name"))
    brand_id = mapping.get("brand_id")
    if not raw_product_name or not brand_id:
        return None
    cursor.execute(
        f"""
        SELECT *
        FROM `{PRODUCT_CANDIDATE_TABLE}`
        WHERE brand_id = %s AND standard_product_name = %s
        LIMIT 1
        FOR UPDATE
        """,
        (brand_id, raw_product_name),
    )
    existing = cursor.fetchone()
    lineage = _merge_product_candidate_lineage(
        existing,
        source_id=int(mapping["source_id"]),
        parsed_row_id=_optional_int(mapping.get("parsed_row_id")),
        raw_product_name=raw_product_name,
        image_id=_clean(mapping.get("image_id")),
    )
    quality_reasons = json.dumps(
        [f"产品标准化状态为 {product_status}，需要人工确认标准产品"],
        ensure_ascii=False,
    )
    if existing:
        cursor.execute(
            f"""
            UPDATE `{PRODUCT_CANDIDATE_TABLE}`
            SET source_ids_json = %s,
                parsed_row_ids_json = %s,
                raw_product_names_json = %s,
                evidence_json = %s,
                review_status = %s,
                quality_reasons_json = CASE
                  WHEN review_status IN ('approved', 'rejected') THEN quality_reasons_json
                  ELSE %s
                END,
                updated_at = NOW()
            WHERE product_id = %s
            """,
            (
                lineage["source_ids_json"],
                lineage["parsed_row_ids_json"],
                lineage["raw_product_names_json"],
                lineage["evidence_json"],
                lineage["review_status"],
                quality_reasons,
                existing["product_id"],
            ),
        )
        return int(existing["product_id"])

    cursor.execute(
        f"""
        INSERT INTO `{PRODUCT_CANDIDATE_TABLE}`(
          brand_id, standard_brand_name, standard_product_name, display_name,
          display_name_rule, candidate_type, active, quality_level, review_status,
          normalized_tags_json, truncation_suspected, quality_reasons_json,
          source_ids_json, parsed_row_ids_json, raw_product_names_json,
          evidence_json, build_batch_id
        ) VALUES(
          %s, %s, %s, %s,
          'ocr_product_name', 'official_name', 0, 'medium', %s,
          JSON_ARRAY(), 0, %s,
          %s, %s, %s,
          %s, %s
        )
        """,
        (
            brand_id,
            _clean(mapping.get("standard_brand_name")) or None,
            raw_product_name,
            raw_product_name,
            lineage["review_status"],
            quality_reasons,
            lineage["source_ids_json"],
            lineage["parsed_row_ids_json"],
            lineage["raw_product_names_json"],
            lineage["evidence_json"],
            uuid.uuid4().hex,
        ),
    )
    return int(cursor.lastrowid)


def seed_brand_master() -> None:
    data = yaml.safe_load(BRAND_MASTER_PATH.read_text(encoding="utf-8")) or {}
    with _connect() as conn:
        with conn.cursor() as cursor:
            for item in data.get("brands") or []:
                if _clean(item.get("status") or "active") != "active":
                    continue
                brand_id = int(item["brand_id"])
                standard_name = _clean(item.get("standard_name"))
                cursor.execute(
                    f"""
                    INSERT INTO `{BRAND_TABLE}`(
                      brand_id, standard_brand_name, origin_type, brand_tier, active
                    ) VALUES(%s, %s, %s, %s, 1)
                    ON DUPLICATE KEY UPDATE
                      standard_brand_name = VALUES(standard_brand_name),
                      origin_type = VALUES(origin_type),
                      brand_tier = VALUES(brand_tier),
                      active = 1
                    """,
                    (
                        brand_id,
                        standard_name,
                        _clean(item.get("origin_type")) or None,
                        _clean(item.get("brand_tier")) or None,
                    ),
                )
                for alias in [standard_name, *(item.get("aliases") or [])]:
                    alias_name = _clean(alias)
                    if not alias_name:
                        continue
                    cursor.execute(
                        f"""
                        INSERT INTO `{BRAND_ALIAS_TABLE}`(
                          brand_id, alias_name, normalized_alias, source, active
                        ) VALUES(%s, %s, %s, 'brand_master_yaml', 1)
                        ON DUPLICATE KEY UPDATE
                          brand_id = VALUES(brand_id),
                          alias_name = VALUES(alias_name),
                          active = 1
                        """,
                        (brand_id, alias_name, normalize_name(alias_name)),
                    )
            for item in data.get("alias_mappings") or []:
                alias_name = _clean(item.get("alias"))
                standard_name = _clean(item.get("standard_name"))
                if not alias_name or not standard_name:
                    continue
                cursor.execute(
                    f"SELECT brand_id FROM `{BRAND_TABLE}` WHERE standard_brand_name = %s",
                    (standard_name,),
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        f"""
                        INSERT INTO `{BRAND_ALIAS_TABLE}`(
                          brand_id, alias_name, normalized_alias, source, active
                        ) VALUES(%s, %s, %s, 'brand_alias_mapping', 1)
                        ON DUPLICATE KEY UPDATE brand_id = VALUES(brand_id), active = 1
                        """,
                        (row["brand_id"], alias_name, normalize_name(alias_name)),
                    )
        conn.commit()


def _load_ocr_row(
    *,
    source_id: int | None = None,
    parsed_row_id: int | None = None,
    file_sha256: str = "",
) -> dict[str, Any]:
    conditions: list[str] = []
    params: list[Any] = []
    if source_id is not None:
        conditions.append("p.source_id = %s")
        params.append(source_id)
    if parsed_row_id is not None:
        conditions.append("p.id = %s")
        params.append(parsed_row_id)
    if file_sha256:
        conditions.append("p.file_sha256 = %s")
        params.append(file_sha256)
    if not conditions:
        raise ValueError("source_id、parsed_row_id、file_sha256 至少提供一个")
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                  p.id AS parsed_row_id,
                  p.source_id,
                  p.file_sha256,
                  p.brand AS raw_brand_name,
                  p.product_name AS raw_product_name,
                  p.ingredient_composition AS raw_ingredient_composition,
                  p.ocr_text
                FROM catfood_ingredient_ocr_parsed p
                WHERE {' OR '.join(conditions)}
                ORDER BY p.id DESC
                LIMIT 1
                """,
                params,
            )
            row = cursor.fetchone()
    if not row:
        raise ValueError("未找到 OCR 解析记录")
    return row


def _upsert_mapping_raw(cursor, row: dict[str, Any], image_id: str = "") -> None:
    cursor.execute(
        f"""
        INSERT INTO `{MAPPING_TABLE}`(
          source_id, parsed_row_id, image_id, file_sha256,
          raw_brand_name, raw_product_name, raw_ingredient_composition
        ) VALUES(%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          parsed_row_id = VALUES(parsed_row_id),
          image_id = COALESCE(VALUES(image_id), image_id),
          file_sha256 = VALUES(file_sha256),
          raw_brand_name = VALUES(raw_brand_name),
          raw_product_name = VALUES(raw_product_name),
          raw_ingredient_composition = VALUES(raw_ingredient_composition)
        """,
        (
            row["source_id"],
            row["parsed_row_id"],
            image_id or None,
            row.get("file_sha256"),
            row.get("raw_brand_name"),
            row.get("raw_product_name"),
            row.get("raw_ingredient_composition"),
        ),
    )


def standardize_brand(payload: dict[str, Any]) -> dict[str, Any]:
    init_standardization_db()
    row = _load_ocr_row(
        source_id=_optional_int(payload.get("source_id")),
        parsed_row_id=_optional_int(payload.get("parsed_row_id")),
        file_sha256=_clean(payload.get("file_sha256")),
    )
    candidates = [
        _clean(row.get("raw_brand_name")),
        _clean(payload.get("brand_name")),
        _clean(row.get("raw_product_name")),
        _clean(row.get("ocr_text"))[:800],
    ]
    exact_rows: dict[int, dict[str, Any]] = {}
    with _connect() as conn:
        with conn.cursor() as cursor:
            _upsert_mapping_raw(cursor, row, _clean(payload.get("image_id")))
            for candidate in candidates[:3]:
                normalized = normalize_name(candidate)
                if not normalized:
                    continue
                cursor.execute(
                    f"""
                    SELECT b.brand_id, b.standard_brand_name
                    FROM `{BRAND_ALIAS_TABLE}` a
                    JOIN `{BRAND_TABLE}` b ON b.brand_id = a.brand_id
                    WHERE a.normalized_alias = %s AND a.active = 1 AND b.active = 1
                    """,
                    (normalized,),
                )
                for match in cursor.fetchall():
                    exact_rows[int(match["brand_id"])] = match
            if not exact_rows:
                haystack = normalize_name(" ".join(candidates))
                cursor.execute(
                    f"""
                    SELECT b.brand_id, b.standard_brand_name, a.normalized_alias
                    FROM `{BRAND_ALIAS_TABLE}` a
                    JOIN `{BRAND_TABLE}` b ON b.brand_id = a.brand_id
                    WHERE a.active = 1 AND b.active = 1
                    """
                )
                for match in cursor.fetchall():
                    alias = match["normalized_alias"]
                    if len(alias) >= 2 and alias in haystack:
                        exact_rows[int(match["brand_id"])] = match
            status = "matched" if len(exact_rows) == 1 else "conflict" if exact_rows else "pending"
            match = next(iter(exact_rows.values())) if status == "matched" else None
            confidence = 1.0 if match and normalize_name(row.get("raw_brand_name")) else 0.85 if match else None
            cursor.execute(
                f"""
                UPDATE `{MAPPING_TABLE}`
                SET brand_id = %s,
                    brand_status = %s,
                    brand_confidence = %s,
                    overall_status = %s,
                    match_evidence_json = JSON_SET(
                      COALESCE(match_evidence_json, JSON_OBJECT()),
                      '$.brand_candidates', CAST(%s AS JSON)
                    )
                WHERE source_id = %s
                """,
                (
                    match["brand_id"] if match else None,
                    status,
                    confidence,
                    status if status != "matched" else "pending",
                    json.dumps(list(exact_rows.values()), ensure_ascii=False),
                    row["source_id"],
                ),
            )
        conn.commit()
    return {
        "ok": True,
        **row,
        "brand_id": match["brand_id"] if match else None,
        "standard_brand_name": match["standard_brand_name"] if match else None,
        "brand_status": status,
        "brand_confidence": confidence,
        "brand_candidates": list(exact_rows.values()),
    }


def resolve_brand_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    init_standardization_db()
    source_id = _required_int(payload.get("source_id"), "source_id")
    brand_id = _optional_int(payload.get("brand_id"))
    brand_name = _clean(payload.get("standard_brand_name") or payload.get("brand_name"))
    with _connect() as conn:
        with conn.cursor() as cursor:
            if brand_id:
                cursor.execute(
                    f"SELECT * FROM `{BRAND_TABLE}` WHERE brand_id = %s AND active = 1",
                    (brand_id,),
                )
            else:
                cursor.execute(
                    f"SELECT * FROM `{BRAND_TABLE}` WHERE standard_brand_name = %s AND active = 1",
                    (brand_name,),
                )
            brand = cursor.fetchone()
            if not brand:
                raise ValueError("标准品牌不存在")
            cursor.execute(
                f"SELECT * FROM `{MAPPING_TABLE}` WHERE source_id = %s FOR UPDATE",
                (source_id,),
            )
            mapping = cursor.fetchone()
            if not mapping:
                row = _load_ocr_row(source_id=source_id)
                _upsert_mapping_raw(cursor, row, _clean(payload.get("image_id")))
            cursor.execute(
                f"""
                UPDATE `{MAPPING_TABLE}`
                SET brand_id = %s,
                    brand_status = 'matched',
                    brand_confidence = 1.0,
                    product_id = NULL,
                    product_status = 'pending',
                    formula_id = NULL,
                    formula_status = 'pending',
                    overall_status = 'pending',
                    reviewer = %s,
                    review_note = %s,
                    reviewed_at = NOW()
                WHERE source_id = %s
                """,
                (
                    brand["brand_id"],
                    _clean(payload.get("reviewer")) or None,
                    _clean(payload.get("review_note")) or None,
                    source_id,
                ),
            )
        conn.commit()
    return {
        "ok": True,
        "source_id": source_id,
        "brand_id": brand["brand_id"],
        "standard_brand_name": brand["standard_brand_name"],
        "brand_status": "matched",
        "overall_status": "pending",
    }


def standardize_product(payload: dict[str, Any]) -> dict[str, Any]:
    init_standardization_db()
    source_id = _required_int(payload.get("source_id"), "source_id")
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM `{MAPPING_TABLE}` WHERE source_id = %s FOR UPDATE",
                (source_id,),
            )
            mapping = cursor.fetchone()
            if not mapping:
                raise ValueError("缺少标准化映射，请先执行品牌标准化")
            if mapping["brand_status"] != "matched" or not mapping.get("brand_id"):
                return {
                    "ok": True,
                    "source_id": source_id,
                    "brand_id": mapping.get("brand_id"),
                    "product_id": None,
                    "product_status": "blocked",
                    "reason": "品牌尚未匹配",
                }
            raw_name = _clean(mapping.get("raw_product_name"))
            normalized = normalize_name(raw_name)
            _, ingredients, fingerprint = normalize_ingredients(
                mapping.get("raw_ingredient_composition")
            )
            nutrition = _load_nutrition_signature(cursor, source_id)
            formula_candidates = _find_formula_product_matches(
                cursor,
                brand_id=int(mapping["brand_id"]),
                ingredients=ingredients,
                fingerprint=fingerprint,
                nutrition=nutrition,
            )
            auto_formula_matches = [
                item for item in formula_candidates if item["auto_match"]
            ]
            auto_product_ids = {
                int(item["product_id"]) for item in auto_formula_matches
            }
            match = None
            match_method = None
            confidence: float | None = None
            if len(auto_product_ids) == 1:
                matched_product_id = next(iter(auto_product_ids))
                cursor.execute(
                    f"""
                    SELECT *
                    FROM `{PRODUCT_TABLE}`
                    WHERE product_id = %s AND brand_id = %s AND active = 1
                    """,
                    (matched_product_id, mapping["brand_id"]),
                )
                match = cursor.fetchone()
                if match:
                    match_method = "formula"
                    confidence = max(
                        item["score"]
                        for item in auto_formula_matches
                        if int(item["product_id"]) == matched_product_id
                    )
                    _upsert_product_alias(
                        cursor,
                        product_id=matched_product_id,
                        brand_id=int(mapping["brand_id"]),
                        alias_name=raw_name,
                        source="formula_auto_match",
                    )
                    cursor.execute(
                        f"""
                        DELETE FROM `{PRODUCT_CANDIDATE_TABLE}`
                        WHERE brand_id = %s
                          AND standard_product_name = %s
                          AND review_status IN ('pending', 'needs_manual_review')
                          AND active = 0
                        """,
                        (mapping["brand_id"], raw_name),
                    )

            name_matches: list[dict[str, Any]] = []
            if not match:
                cursor.execute(
                    f"""
                    SELECT p.*
                    FROM `{PRODUCT_ALIAS_TABLE}` a
                    JOIN `{PRODUCT_TABLE}` p ON p.product_id = a.product_id
                    WHERE a.brand_id = %s
                      AND a.normalized_alias = %s
                      AND a.active = 1
                      AND p.active = 1
                    """,
                    (mapping["brand_id"], normalized),
                )
                name_matches = cursor.fetchall()
                if not name_matches and normalized:
                    cursor.execute(
                        f"""
                        SELECT *
                        FROM `{PRODUCT_TABLE}`
                        WHERE brand_id = %s AND active = 1
                        """,
                        (mapping["brand_id"],),
                    )
                    name_matches = [
                        item
                        for item in cursor.fetchall()
                        if normalize_name(item["standard_product_name"]) == normalized
                        or normalize_name(item["display_name"]) == normalized
                    ]
                if len(name_matches) == 1:
                    match = name_matches[0]
                    match_method = "name"
                    confidence = 1.0

            if match:
                status = "matched"
            elif len(auto_product_ids) > 1 or len(name_matches) > 1:
                status = "conflict"
            else:
                status = "pending"
            candidate_product_id = None
            if status != "matched":
                brand_name = ""
                cursor.execute(
                    f"SELECT standard_brand_name FROM `{BRAND_TABLE}` WHERE brand_id = %s",
                    (mapping["brand_id"],),
                )
                brand_row = cursor.fetchone()
                if brand_row:
                    brand_name = _clean(brand_row.get("standard_brand_name"))
                candidate_product_id = _upsert_product_review_candidate(
                    cursor,
                    mapping={
                        **mapping,
                        "standard_brand_name": brand_name,
                    },
                    product_status=status,
                )
            cursor.execute(
                f"""
                UPDATE `{MAPPING_TABLE}`
                SET product_id = %s,
                    product_status = %s,
                    product_confidence = %s,
                    overall_status = %s,
                    match_evidence_json = JSON_SET(
                      COALESCE(match_evidence_json, JSON_OBJECT()),
                      '$.product_candidates', CAST(%s AS JSON),
                      '$.formula_product_candidates', CAST(%s AS JSON),
                      '$.product_match_method', CAST(%s AS JSON)
                    )
                WHERE source_id = %s
                """,
                (
                    match["product_id"] if match else None,
                    status,
                    confidence,
                    status if status != "matched" else "pending",
                    json.dumps(name_matches, ensure_ascii=False, default=str),
                    json.dumps(formula_candidates, ensure_ascii=False, default=str),
                    json.dumps(match_method, ensure_ascii=False),
                    source_id,
                ),
            )
        conn.commit()
    result = {
        "ok": True,
        "source_id": source_id,
        "brand_id": mapping["brand_id"],
        "product_id": match["product_id"] if match else None,
        "standard_product_name": match["standard_product_name"] if match else None,
        "display_name": match["display_name"] if match else None,
        "product_status": status,
        "product_confidence": confidence,
        "product_match_method": match_method,
        "product_candidates": name_matches,
        "formula_product_candidates": formula_candidates,
        "review_candidate_id": candidate_product_id,
    }
    return result


INGREDIENT_SYNONYMS = {
    "鲑鱼": "三文鱼",
    "鮭魚": "三文鱼",
    "冻鸡胸肉": "鸡胸肉",
    "鲜鸡胸肉": "鸡胸肉",
    "脱水鸡肉": "鸡肉粉",
    "鸡肉干": "鸡肉粉",
}


def normalize_ingredients(value: Any) -> tuple[str, list[str], str]:
    text = _clean(value)
    text = re.split(r"添加剂组成|添加剂|产品成分分析|营养分析|保证值", text, maxsplit=1)[0]
    text = re.sub(r"^(?:原料组成|配料组成)\s*[:：]?", "", text)
    ingredients: list[str] = []
    for part in re.split(r"[,，、;；。\n]+", text):
        item = re.sub(r"\([^)]*\)|（[^）]*）", "", part)
        item = re.sub(r"\d+(?:\.\d+)?\s*%", "", item)
        item = _clean(item).strip(" :：")
        if not item or len(item) > 80:
            continue
        item = INGREDIENT_SYNONYMS.get(item, item)
        ingredients.append(item)
    normalized = "，".join(ingredients)
    fingerprint = hashlib.sha256(
        json.dumps(ingredients, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return normalized, ingredients, fingerprint


def _rank_weight(index: int) -> float:
    return 1.0 / (index + 1)


def ordered_ingredient_similarity(left: list[str], right: list[str]) -> dict[str, float]:
    if not left or not right:
        return {
            "score": 0.0,
            "set_similarity": 0.0,
            "weighted_position_similarity": 0.0,
            "weighted_order_similarity": 0.0,
            "top5_exact_ratio": 0.0,
        }

    left_set = set(left)
    right_set = set(right)
    set_similarity = len(left_set & right_set) / len(left_set | right_set)

    max_size = max(len(left), len(right))
    position_total = sum(_rank_weight(index) for index in range(max_size))
    position_score = 0.0
    for index in range(max_size):
        if index < len(left) and index < len(right) and left[index] == right[index]:
            position_score += _rank_weight(index)
    weighted_position_similarity = position_score / position_total if position_total else 0.0

    shared = left_set & right_set
    left_positions = {name: index for index, name in enumerate(left)}
    right_positions = {name: index for index, name in enumerate(right)}
    order_total = sum(_rank_weight(left_positions[name]) for name in shared)
    order_score = 0.0
    for name in shared:
        left_index = left_positions[name]
        right_index = right_positions[name]
        distance = abs(left_index - right_index)
        order_score += _rank_weight(left_index) / (1.0 + distance)
    weighted_order_similarity = order_score / order_total if order_total else 0.0

    top_size = min(5, len(left), len(right))
    top5_exact_ratio = (
        sum(left[index] == right[index] for index in range(top_size)) / top_size
        if top_size
        else 0.0
    )
    score = (
        weighted_position_similarity * 0.5
        + weighted_order_similarity * 0.3
        + set_similarity * 0.2
    )
    return {
        "score": round(score, 5),
        "set_similarity": round(set_similarity, 5),
        "weighted_position_similarity": round(weighted_position_similarity, 5),
        "weighted_order_similarity": round(weighted_order_similarity, 5),
        "top5_exact_ratio": round(top5_exact_ratio, 5),
    }


def ingredient_similarity(left: list[str], right: list[str]) -> float:
    return ordered_ingredient_similarity(left, right)["score"]


def _load_nutrition_signature(cursor, source_id: int) -> dict[str, dict[str, Any]]:
    cursor.execute("SHOW TABLES LIKE 'product_guarantee'")
    if not cursor.fetchone():
        return {}
    cursor.execute(
        """
        SELECT metric_name, operator_symbol, metric_value, metric_unit, basis
        FROM product_guarantee
        WHERE source_id = %s
        ORDER BY metric_name, operator_symbol, metric_unit
        """,
        (source_id,),
    )
    signature: dict[str, dict[str, Any]] = {}
    for row in cursor.fetchall():
        metric_name = _clean(row.get("metric_name"))
        unit = _clean(row.get("metric_unit")).lower()
        operator = _clean(row.get("operator_symbol"))
        basis = _clean(row.get("basis")).lower()
        if not metric_name or not unit:
            continue
        key = f"{metric_name}|{operator}|{unit}|{basis}"
        signature[key] = {
            "metric_name": metric_name,
            "operator": operator,
            "value": float(row["metric_value"]),
            "unit": unit,
            "basis": basis,
        }
    return signature


def nutrition_similarity(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    common_keys = sorted(set(left) & set(right))
    if not common_keys:
        return {"score": 0.0, "comparable_count": 0, "matched_count": 0, "differences": []}
    matched = 0
    differences = []
    for key in common_keys:
        left_value = float(left[key]["value"])
        right_value = float(right[key]["value"])
        tolerance = max(0.05, abs(left_value) * 0.01, abs(right_value) * 0.01)
        difference = abs(left_value - right_value)
        if difference <= tolerance:
            matched += 1
        else:
            differences.append(
                {
                    "metric": left[key]["metric_name"],
                    "left": left_value,
                    "right": right_value,
                    "tolerance": round(tolerance, 4),
                }
            )
    return {
        "score": round(matched / len(common_keys), 5),
        "comparable_count": len(common_keys),
        "matched_count": matched,
        "differences": differences,
    }


def _formula_nutrition_signature(cursor, formula: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = formula.get("nutrition_json")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            return parsed
    cursor.execute(
        f"""
        SELECT source_id
        FROM `{MAPPING_TABLE}`
        WHERE formula_id = %s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (formula["formula_id"],),
    )
    mapping = cursor.fetchone()
    return _load_nutrition_signature(cursor, int(mapping["source_id"])) if mapping else {}


def _find_formula_product_matches(
    cursor,
    *,
    brand_id: int,
    ingredients: list[str],
    fingerprint: str,
    nutrition: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not ingredients:
        return []
    cursor.execute(
        f"""
        SELECT f.*, p.brand_id, p.standard_product_name, p.display_name
        FROM `{FORMULA_TABLE}` f
        JOIN `{PRODUCT_TABLE}` p ON p.product_id = f.product_id
        WHERE p.brand_id = %s
          AND p.active = 1
          AND f.status = 'active'
        ORDER BY f.product_id, f.formula_version DESC
        """,
        (brand_id,),
    )
    candidates = []
    products_with_formulas: set[int] = set()
    for formula in cursor.fetchall():
        products_with_formulas.add(int(formula["product_id"]))
        existing_items = formula.get("normalized_ingredients_json")
        if isinstance(existing_items, str):
            existing_items = json.loads(existing_items or "[]")
        ingredient_evidence = ordered_ingredient_similarity(ingredients, existing_items or [])
        existing_nutrition = _formula_nutrition_signature(cursor, formula)
        nutrition_evidence = nutrition_similarity(nutrition, existing_nutrition)
        fingerprint_exact = formula.get("ingredient_fingerprint") == fingerprint
        nutrition_strong = (
            nutrition_evidence["comparable_count"] >= 4
            and nutrition_evidence["score"] >= 0.95
        )
        ingredient_strong = (
            fingerprint_exact
            or (
                ingredient_evidence["score"] >= 0.96
                and ingredient_evidence["top5_exact_ratio"] >= 0.8
            )
        )
        score = ingredient_evidence["score"] * 0.75 + nutrition_evidence["score"] * 0.25
        candidates.append(
            {
                "product_id": int(formula["product_id"]),
                "formula_id": int(formula["formula_id"]),
                "formula_version": int(formula["formula_version"]),
                "standard_product_name": formula["standard_product_name"],
                "display_name": formula["display_name"],
                "fingerprint_exact": fingerprint_exact,
                "ingredient_evidence": ingredient_evidence,
                "nutrition_evidence": nutrition_evidence,
                "auto_match": ingredient_strong and nutrition_strong,
                "score": round(score, 5),
                "evidence_source": "standard_formula",
            }
        )

    cursor.execute(f"SHOW TABLES LIKE '{PRODUCT_CANDIDATE_TABLE}'")
    if cursor.fetchone():
        cursor.execute(
            f"""
            SELECT
              p.product_id,
              p.standard_product_name,
              p.display_name,
              c.source_ids_json
            FROM `{PRODUCT_TABLE}` p
            JOIN `{PRODUCT_CANDIDATE_TABLE}` c
              ON c.product_id = p.source_candidate_id
            WHERE p.brand_id = %s
              AND p.active = 1
              AND c.review_status = 'approved'
            """,
            (brand_id,),
        )
        for product in cursor.fetchall():
            product_id = int(product["product_id"])
            if product_id in products_with_formulas:
                continue
            for candidate_source_id in _json_list(product.get("source_ids_json")):
                try:
                    candidate_source_id = int(candidate_source_id)
                except (TypeError, ValueError):
                    continue
                cursor.execute(
                    """
                    SELECT ingredient_composition
                    FROM catfood_ingredient_ocr_parsed
                    WHERE source_id = %s
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (candidate_source_id,),
                )
                parsed = cursor.fetchone()
                _, existing_items, existing_fingerprint = normalize_ingredients(
                    (parsed or {}).get("ingredient_composition")
                )
                if not existing_items:
                    continue
                ingredient_evidence = ordered_ingredient_similarity(
                    ingredients, existing_items
                )
                nutrition_evidence = nutrition_similarity(
                    nutrition,
                    _load_nutrition_signature(cursor, candidate_source_id),
                )
                fingerprint_exact = existing_fingerprint == fingerprint
                ingredient_strong = (
                    fingerprint_exact
                    or (
                        ingredient_evidence["score"] >= 0.96
                        and ingredient_evidence["top5_exact_ratio"] >= 0.8
                    )
                )
                nutrition_strong = (
                    nutrition_evidence["comparable_count"] >= 4
                    and nutrition_evidence["score"] >= 0.95
                )
                score = (
                    ingredient_evidence["score"] * 0.75
                    + nutrition_evidence["score"] * 0.25
                )
                candidates.append(
                    {
                        "product_id": product_id,
                        "formula_id": None,
                        "formula_version": None,
                        "standard_product_name": product["standard_product_name"],
                        "display_name": product["display_name"],
                        "fingerprint_exact": fingerprint_exact,
                        "ingredient_evidence": ingredient_evidence,
                        "nutrition_evidence": nutrition_evidence,
                        "auto_match": ingredient_strong and nutrition_strong,
                        "score": round(score, 5),
                        "evidence_source": "approved_candidate_ocr",
                        "evidence_source_id": candidate_source_id,
                    }
                )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def _upsert_product_alias(
    cursor,
    *,
    product_id: int,
    brand_id: int,
    alias_name: str,
    source: str,
) -> None:
    normalized_alias = normalize_name(alias_name)
    if not normalized_alias:
        return
    cursor.execute(
        f"""
        INSERT INTO `{PRODUCT_ALIAS_TABLE}`(
          product_id, brand_id, alias_name, normalized_alias, source, active
        ) VALUES(%s, %s, %s, %s, %s, 1)
        ON DUPLICATE KEY UPDATE
          product_id = VALUES(product_id),
          alias_name = VALUES(alias_name),
          source = VALUES(source),
          active = 1
        """,
        (product_id, brand_id, alias_name, normalized_alias, source),
    )


def standardize_formula(payload: dict[str, Any]) -> dict[str, Any]:
    init_standardization_db()
    source_id = _required_int(payload.get("source_id"), "source_id")
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM `{MAPPING_TABLE}` WHERE source_id = %s FOR UPDATE",
                (source_id,),
            )
            mapping = cursor.fetchone()
            if not mapping:
                raise ValueError("缺少标准化映射")
            if mapping["product_status"] != "matched" or not mapping.get("product_id"):
                return {
                    "ok": True,
                    "source_id": source_id,
                    "formula_id": None,
                    "formula_status": "blocked",
                    "reason": "产品尚未匹配",
                }
            normalized, ingredients, fingerprint = normalize_ingredients(
                mapping.get("raw_ingredient_composition")
            )
            nutrition = _load_nutrition_signature(cursor, source_id)
            confidence: float | None = None
            conflict_candidates: list[dict[str, Any]] = []
            if not ingredients:
                status = "pending"
                match = None
            else:
                cursor.execute(
                    f"""
                    SELECT *
                    FROM `{FORMULA_TABLE}`
                    WHERE product_id = %s AND ingredient_fingerprint = %s
                    """,
                    (mapping["product_id"], fingerprint),
                )
                match = cursor.fetchone()
                if match:
                    nutrition_evidence = nutrition_similarity(
                        nutrition,
                        _formula_nutrition_signature(cursor, match),
                    )
                    nutrition_compatible = (
                        nutrition_evidence["comparable_count"] < 4
                        or nutrition_evidence["score"] >= 0.95
                    )
                    if nutrition_compatible:
                        confidence = 1.0
                    else:
                        conflict_candidates.append(
                            {
                                "formula_id": match["formula_id"],
                                "formula_version": match["formula_version"],
                                "fingerprint_exact": True,
                                "ingredient_evidence": ordered_ingredient_similarity(
                                    ingredients,
                                    json.loads(match.get("normalized_ingredients_json") or "[]")
                                    if isinstance(match.get("normalized_ingredients_json"), str)
                                    else match.get("normalized_ingredients_json") or [],
                                ),
                                "nutrition_evidence": nutrition_evidence,
                                "similarity": 1.0,
                            }
                        )
                        match = None
                if not match:
                    cursor.execute(
                        f"""
                        SELECT *
                        FROM `{FORMULA_TABLE}`
                        WHERE product_id = %s AND status = 'active'
                        ORDER BY formula_version DESC
                        """,
                        (mapping["product_id"],),
                    )
                    existing_formulas = cursor.fetchall()
                    for existing in existing_formulas:
                        existing_items = existing.get("normalized_ingredients_json")
                        if isinstance(existing_items, str):
                            existing_items = json.loads(existing_items or "[]")
                        ingredient_evidence = ordered_ingredient_similarity(
                            ingredients, existing_items or []
                        )
                        nutrition_evidence = nutrition_similarity(
                            nutrition,
                            _formula_nutrition_signature(cursor, existing),
                        )
                        conflict_candidates.append(
                            {
                                "formula_id": existing["formula_id"],
                                "formula_version": existing["formula_version"],
                                "fingerprint_exact": existing.get("ingredient_fingerprint") == fingerprint,
                                "ingredient_evidence": ingredient_evidence,
                                "nutrition_evidence": nutrition_evidence,
                                "similarity": ingredient_evidence["score"],
                            }
                        )
                    conflict_candidates.sort(key=lambda item: item["similarity"], reverse=True)
                    best_candidate = conflict_candidates[0] if conflict_candidates else None
                    if (
                        best_candidate
                        and best_candidate["ingredient_evidence"]["score"] >= 0.96
                        and best_candidate["ingredient_evidence"]["top5_exact_ratio"] >= 0.8
                        and best_candidate["nutrition_evidence"]["comparable_count"] >= 4
                        and best_candidate["nutrition_evidence"]["score"] >= 0.95
                    ):
                        cursor.execute(
                            f"SELECT * FROM `{FORMULA_TABLE}` WHERE formula_id = %s",
                            (best_candidate["formula_id"],),
                        )
                        match = cursor.fetchone()
                        confidence = round(
                            best_candidate["ingredient_evidence"]["score"] * 0.75
                            + best_candidate["nutrition_evidence"]["score"] * 0.25,
                            5,
                        )
                if not match and not conflict_candidates:
                    cursor.execute(
                        f"""
                        SELECT COALESCE(MAX(formula_version), 0) + 1 AS next_version
                        FROM `{FORMULA_TABLE}`
                        WHERE product_id = %s
                        """,
                        (mapping["product_id"],),
                    )
                    version = int(cursor.fetchone()["next_version"])
                    cursor.execute(
                        f"UPDATE `{FORMULA_TABLE}` SET is_current = 0 WHERE product_id = %s",
                        (mapping["product_id"],),
                    )
                    cursor.execute(
                        f"""
                        INSERT INTO `{FORMULA_TABLE}`(
                          product_id, formula_version, raw_ingredient_example,
                          normalized_ingredient_composition, normalized_ingredients_json,
                          ingredient_fingerprint, nutrition_json, is_current, status
                        ) VALUES(%s, %s, %s, %s, %s, %s, %s, 1, 'active')
                        """,
                        (
                            mapping["product_id"],
                            version,
                            mapping.get("raw_ingredient_composition"),
                            normalized,
                            json.dumps(ingredients, ensure_ascii=False),
                            fingerprint,
                            json.dumps(nutrition, ensure_ascii=False),
                        ),
                    )
                    formula_id = cursor.lastrowid
                    cursor.execute(
                        f"SELECT * FROM `{FORMULA_TABLE}` WHERE formula_id = %s",
                        (formula_id,),
                    )
                    match = cursor.fetchone()
                    confidence = 1.0
                status = "matched" if match else "conflict"
            overall = "matched" if status == "matched" else status
            cursor.execute(
                f"""
                UPDATE `{MAPPING_TABLE}`
                SET formula_id = %s,
                    formula_status = %s,
                    formula_confidence = %s,
                    overall_status = %s
                WHERE source_id = %s
                """,
                (
                    match["formula_id"] if match else None,
                    status,
                    confidence,
                    overall,
                    source_id,
                ),
            )
        conn.commit()
    result = {
        "ok": True,
        "source_id": source_id,
        "brand_id": mapping["brand_id"],
        "product_id": mapping["product_id"],
        "formula_id": match["formula_id"] if match else None,
        "formula_version": match["formula_version"] if match else None,
        "formula_status": status,
        "formula_confidence": confidence,
        "ingredient_fingerprint": fingerprint if ingredients else None,
        "normalized_ingredients": ingredients,
        "overall_status": overall,
        "formula_candidates": conflict_candidates if ingredients else [],
    }
    if result["formula_id"]:
        result["formula_input"] = build_formula_feature_input(
            formula_id=int(result["formula_id"]),
            apply=True,
        )
    return result


def resolve_formula_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve a formula conflict by reusing a formula or creating a new version."""
    init_standardization_db()
    source_id = _required_int(payload.get("source_id"), "source_id")
    action = _clean(payload.get("action"))
    if action not in {"reuse", "create_new"}:
        raise ValueError("action 必须是 reuse 或 create_new")
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM `{MAPPING_TABLE}` WHERE source_id = %s FOR UPDATE",
                (source_id,),
            )
            mapping = cursor.fetchone()
            if not mapping or not mapping.get("product_id"):
                raise ValueError("映射不存在或产品尚未匹配")
            normalized, ingredients, fingerprint = normalize_ingredients(
                mapping.get("raw_ingredient_composition")
            )
            nutrition = _load_nutrition_signature(cursor, source_id)
            if not ingredients:
                raise ValueError("原料组成为空，无法创建配方")
            if action == "reuse":
                formula_id = _required_int(payload.get("formula_id"), "formula_id")
                cursor.execute(
                    f"""
                    SELECT * FROM `{FORMULA_TABLE}`
                    WHERE formula_id = %s AND product_id = %s
                    """,
                    (formula_id, mapping["product_id"]),
                )
                formula = cursor.fetchone()
                if not formula:
                    raise ValueError("配方不存在或不属于当前产品")
            else:
                cursor.execute(
                    f"""
                    SELECT COALESCE(MAX(formula_version), 0) + 1 AS next_version
                    FROM `{FORMULA_TABLE}` WHERE product_id = %s
                    """,
                    (mapping["product_id"],),
                )
                version = int(cursor.fetchone()["next_version"])
                cursor.execute(
                    f"UPDATE `{FORMULA_TABLE}` SET is_current = 0 WHERE product_id = %s",
                    (mapping["product_id"],),
                )
                cursor.execute(
                    f"""
                    INSERT INTO `{FORMULA_TABLE}`(
                      product_id, formula_version, raw_ingredient_example,
                      normalized_ingredient_composition, normalized_ingredients_json,
                      ingredient_fingerprint, nutrition_json, is_current, status
                    ) VALUES(%s, %s, %s, %s, %s, %s, %s, 1, 'active')
                    """,
                    (
                        mapping["product_id"],
                        version,
                        mapping.get("raw_ingredient_composition"),
                        normalized,
                        json.dumps(ingredients, ensure_ascii=False),
                        fingerprint,
                        json.dumps(nutrition, ensure_ascii=False),
                    ),
                )
                formula_id = cursor.lastrowid
                cursor.execute(
                    f"SELECT * FROM `{FORMULA_TABLE}` WHERE formula_id = %s",
                    (formula_id,),
                )
                formula = cursor.fetchone()
            cursor.execute(
                f"""
                UPDATE `{MAPPING_TABLE}`
                SET formula_id = %s,
                    formula_status = 'matched',
                    formula_confidence = 1.0,
                    overall_status = 'matched',
                    reviewer = %s,
                    review_note = %s,
                    reviewed_at = NOW()
                WHERE source_id = %s
                """,
                (
                    formula["formula_id"],
                    _clean(payload.get("reviewer")) or None,
                    _clean(payload.get("review_note")) or None,
                    source_id,
                ),
            )
        conn.commit()
    result = {
        "ok": True,
        "source_id": source_id,
        "product_id": mapping["product_id"],
        "formula_id": formula["formula_id"],
        "formula_version": formula["formula_version"],
        "formula_status": "matched",
        "overall_status": "matched",
    }
    result["formula_input"] = build_formula_feature_input(
        formula_id=int(formula["formula_id"]),
        apply=True,
    )
    return result


def get_standard_mapping(source_id: int) -> dict[str, Any] | None:
    init_standardization_db()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                  m.*,
                  b.standard_brand_name,
                  p.standard_product_name,
                  p.display_name,
                  f.formula_version,
                  f.ingredient_fingerprint
                FROM `{MAPPING_TABLE}` m
                LEFT JOIN `{BRAND_TABLE}` b ON b.brand_id = m.brand_id
                LEFT JOIN `{PRODUCT_TABLE}` p ON p.product_id = m.product_id
                LEFT JOIN `{FORMULA_TABLE}` f ON f.formula_id = m.formula_id
                WHERE m.source_id = %s
                """,
                (int(source_id),),
            )
            return cursor.fetchone()


def list_standard_brands(*, query: str = "", limit: int = 300) -> list[dict[str, Any]]:
    init_standardization_db()
    limit = max(1, min(int(limit or 300), 500))
    where = "WHERE active = 1"
    params: list[Any] = []
    if query:
        where += " AND standard_brand_name LIKE %s"
        params.append(f"%{_clean(query)}%")
    params.append(limit)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT brand_id, standard_brand_name
                FROM `{BRAND_TABLE}`
                {where}
                ORDER BY standard_brand_name
                LIMIT %s
                """,
                params,
            )
            return cursor.fetchall()


def resolve_standard_brand(value: Any) -> dict[str, Any] | None:
    name = _clean(value)
    normalized = normalize_name(name)
    if not normalized:
        return None
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT b.brand_id, b.standard_brand_name
                FROM `{BRAND_TABLE}` b
                WHERE b.standard_brand_name = %s AND b.active = 1
                LIMIT 1
                """,
                (name,),
            )
            row = cursor.fetchone()
            if row:
                return row
            cursor.execute(
                f"""
                SELECT b.brand_id, b.standard_brand_name
                FROM `{BRAND_ALIAS_TABLE}` a
                JOIN `{BRAND_TABLE}` b ON b.brand_id = a.brand_id
                WHERE a.normalized_alias = %s
                  AND a.active = 1
                  AND b.active = 1
                LIMIT 1
                """,
                (normalized,),
            )
            row = cursor.fetchone()
            if row:
                return row
            if len(normalized) >= 2:
                cursor.execute(
                    f"""
                    SELECT DISTINCT b.brand_id, b.standard_brand_name
                    FROM `{BRAND_ALIAS_TABLE}` a
                    JOIN `{BRAND_TABLE}` b ON b.brand_id = a.brand_id
                    WHERE a.active = 1
                      AND b.active = 1
                      AND (
                        a.normalized_alias LIKE %s
                        OR %s LIKE CONCAT('%%', a.normalized_alias, '%%')
                      )
                    """,
                    (f"%{normalized}%", normalized),
                )
                matches = cursor.fetchall()
                if len(matches) == 1:
                    return matches[0]
            return None


def _optional_int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _required_int(value: Any, field: str) -> int:
    result = _optional_int(value)
    if result is None:
        raise ValueError(f"{field} 不能为空")
    return result
