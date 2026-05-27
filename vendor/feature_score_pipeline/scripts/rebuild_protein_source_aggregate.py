#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from brand_normalizer import build_product_key as build_corrected_product_key
from brand_normalizer import correct_brand


NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
SPLIT_SOURCE_RE = re.compile(r"[、,，/|]+")
CSV_LABELING_PROJECT = Path(
    os.getenv(
        "CSV_LABELING_PROJECT",
        "/Users/yoghourt/anaconda3/envs/comment_labeler_env/csv_mysql_labeling_project",
    )
)

ANIMAL_SOURCE_LEVEL1_ORDER = ["禽类", "鱼类", "红肉类", "蛋类"]
ANIMAL_SOURCE_LEVEL2_TO_LEVEL1 = {
    "鸡": "禽类",
    "火鸡": "禽类",
    "鸭": "禽类",
    "鹌鹑": "禽类",
    "鲱鱼": "鱼类",
    "鳕鱼": "鱼类",
    "比目鱼": "鱼类",
    "岩鱼": "鱼类",
    "白鱼": "鱼类",
    "鱼": "鱼类",
    "牛": "红肉类",
    "羊": "红肉类",
    "鹿": "红肉类",
    "兔": "红肉类",
    "猪": "红肉类",
    "鸡蛋": "蛋类",
    "鸭蛋": "蛋类",
}
FISH_SUBTYPE_ORDER = [
    "鲱鱼",
    "鳕鱼",
    "比目鱼",
    "岩鱼",
    "鳟鱼",
    "海洋鱼",
    "金枪鱼",
    "三文鱼",
    "白鱼",
    "鲭鱼",
    "沙丁鱼",
    "马鲛鱼",
    "白斑狗鱼",
    "竹荚鱼",
    "鲻鱼",
    "凤尾鱼",
]


def _safe_name(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} cannot be empty")
    if not NAME_RE.match(value):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _fq(db_name: str, table_name: str) -> str:
    return f"`{db_name}`.`{table_name}`"


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _split_source_tokens(value: Any) -> list[str]:
    text_value = _clean_text(value)
    if not text_value:
        return []
    return [token.strip() for token in SPLIT_SOURCE_RE.split(text_value) if token.strip()]


def _normalize_source_token(token: str) -> Optional[str]:
    cleaned = token.strip()
    if not cleaned:
        return None

    if "鸡蛋" in cleaned:
        return "鸡蛋"
    if "鸭蛋" in cleaned:
        return "鸭蛋"

    if "火鸡" in cleaned:
        return "火鸡"
    if "鹌鹑" in cleaned:
        return "鹌鹑"
    if "鸭" in cleaned and "蛋" not in cleaned:
        return "鸭"
    if "鸡" in cleaned and "蛋" not in cleaned:
        return "鸡"

    if "鱼" in cleaned:
        return "鱼"

    if "牛" in cleaned:
        return "牛"
    if "山羊" in cleaned:
        return "羊"
    if "羊" in cleaned:
        return "羊"
    if "鹿" in cleaned:
        return "鹿"
    if "兔" in cleaned:
        return "兔"
    if "猪" in cleaned:
        return "猪"

    return None


def _extract_fish_subtypes(*, animal_sources: Any, protein_source_details: Any) -> list[str]:
    text_pool = " ".join(
        [
            _clean_text(animal_sources) or "",
            _clean_text(protein_source_details) or "",
        ]
    )
    found: list[str] = []
    if "鲱" in text_pool:
        found.append("鲱鱼")
    if "鳕" in text_pool:
        found.append("鳕鱼")
    if "比目" in text_pool or "鳎" in text_pool:
        found.append("比目鱼")
    if "岩鱼" in text_pool:
        found.append("岩鱼")
    if "鳟" in text_pool:
        found.append("鳟鱼")
    if "海洋鱼" in text_pool:
        found.append("海洋鱼")
    if "金枪" in text_pool or "吞拿" in text_pool:
        found.append("金枪鱼")
    if "三文鱼粉" in text_pool or "三文鱼" in text_pool or "三文" in text_pool or ("鲑鱼" in text_pool and "白鲑" not in text_pool):
        found.append("三文鱼")
    if "白鱼粉" in text_pool or "白鱼" in text_pool or "白鲑" in text_pool:
        found.append("白鱼")
    if "鲭鱼粉" in text_pool or "鲭鱼" in text_pool:
        found.append("鲭鱼")
    if "沙丁鱼粉" in text_pool or "沙丁鱼" in text_pool:
        found.append("沙丁鱼")
    if "马鲛鱼" in text_pool:
        found.append("马鲛鱼")
    if "白斑狗鱼" in text_pool:
        found.append("白斑狗鱼")
    if "竹荚鱼" in text_pool:
        found.append("竹荚鱼")
    if "鲻鱼" in text_pool:
        found.append("鲻鱼")
    if "凤尾鱼" in text_pool or "鳀鱼" in text_pool:
        found.append("凤尾鱼")
    unique: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return sorted(unique, key=lambda item: FISH_SUBTYPE_ORDER.index(item))


def _classify_animal_sources(animal_sources: Any, protein_source_details: Any) -> tuple[Optional[str], Optional[str]]:
    level2_tokens: list[str] = []
    seen_level2: set[str] = set()
    level1_set: set[str] = set()
    has_fish_generic = False

    for raw_token in _split_source_tokens(animal_sources):
        normalized = _normalize_source_token(raw_token)
        if not normalized:
            continue
        if normalized == "鱼":
            has_fish_generic = True
            continue
        if normalized not in seen_level2:
            seen_level2.add(normalized)
            level2_tokens.append(normalized)
        level1 = ANIMAL_SOURCE_LEVEL2_TO_LEVEL1.get(normalized)
        if level1:
            level1_set.add(level1)

    fish_subtypes = _extract_fish_subtypes(
        animal_sources=animal_sources,
        protein_source_details=protein_source_details,
    )
    if fish_subtypes:
        level1_set.add("鱼类")
        for fish in fish_subtypes:
            if fish not in seen_level2:
                seen_level2.add(fish)
                level2_tokens.append(fish)
    elif has_fish_generic:
        if "鱼" not in seen_level2:
            seen_level2.add("鱼")
            level2_tokens.append("鱼")
        level1_set.add("鱼类")

    ordered_level1 = [category for category in ANIMAL_SOURCE_LEVEL1_ORDER if category in level1_set]
    level1_text = "、".join(ordered_level1) if ordered_level1 else None
    level2_text = "、".join(level2_tokens) if level2_tokens else None
    return level1_text, level2_text


def _count_multi_values(value: Any) -> Optional[int]:
    text_value = _clean_text(value)
    if not text_value:
        return None
    parts = [part.strip() for part in re.split(r"\s*\|\s*", text_value) if part.strip()]
    if not parts:
        return None
    return len(dict.fromkeys(parts))


def _limit_table_name(base: str, suffix: str, batch_id: str) -> str:
    max_len = 64
    reserved = len(suffix) + len(batch_id) + 2
    trimmed = base[: max_len - reserved]
    return f"{trimmed}__{suffix}_{batch_id}"


def make_engine(args: argparse.Namespace) -> Engine:
    if args.dsn:
        return create_engine(args.dsn, future=True)

    host = args.host or os.getenv("MYSQL_HOST") or "127.0.0.1"
    port = int(args.port or os.getenv("MYSQL_PORT") or 3306)
    user = args.user or os.getenv("MYSQL_USER")
    password = args.password if args.password is not None else os.getenv("MYSQL_PASSWORD")
    charset = args.charset or os.getenv("MYSQL_CHARSET") or "utf8mb4"

    if not user:
        raise ValueError("missing MySQL user; pass --user or --dsn")

    url = f"mysql+pymysql://{user}:{password or ''}@{host}:{port}/?charset={charset}"
    return create_engine(url, future=True)


def _table_exists(engine: Engine, db_name: str, table_name: str) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                    """
                ),
                {"schema_name": db_name, "table_name": table_name},
            ).scalar()
        )


def _target_table_ddl(table_name: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS `{table_name}` (
      `source_id` BIGINT NOT NULL,
      `product_key` VARCHAR(255) DEFAULT NULL,
      `guarantee_product_id` BIGINT DEFAULT NULL,
      `brand_name` VARCHAR(255) DEFAULT NULL,
      `product_name` VARCHAR(255) DEFAULT NULL,
      `animal_sources` TEXT,
      `animal_source_level1_categories` VARCHAR(255) DEFAULT NULL,
      `animal_source_level2_sources` TEXT,
      `protein_source_details` TEXT,
      `primary_meat_source_species` VARCHAR(255) DEFAULT NULL,
      `secondary_meat_source_species` VARCHAR(255) DEFAULT NULL,
      `primary_meat_source_type` VARCHAR(255) DEFAULT NULL,
      `secondary_meat_source_type` VARCHAR(255) DEFAULT NULL,
      `primary_meat_source_count` INT DEFAULT NULL,
      `secondary_meat_source_count` INT DEFAULT NULL,
      `protein_source_origin` VARCHAR(255) DEFAULT NULL,
      `plant_protein_labels` TEXT,
      `guarantee_crude_protein_metric_name` VARCHAR(100) DEFAULT NULL,
      `guarantee_crude_protein_value` DECIMAL(8,2) DEFAULT NULL,
      `guarantee_crude_protein_unit` VARCHAR(50) DEFAULT NULL,
      `aggregated_at` TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (`source_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """


def load_rows(
    engine: Engine,
    *,
    source_db: str,
    source_table: str,
    parsed_db: str,
    parsed_table: str,
    feature_db: str,
    feature_table: str,
    guarantee_db: str,
    product_info_table: str,
    product_guarantee_table: str,
) -> list[dict[str, Any]]:
    source_fq = _fq(source_db, source_table)
    parsed_fq = _fq(parsed_db, parsed_table)
    product_info_fq = _fq(guarantee_db, product_info_table)
    product_guarantee_fq = _fq(guarantee_db, product_guarantee_table)

    with engine.connect() as conn:
        feature_table_exists = bool(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                    """
                ),
                {"schema_name": feature_db, "table_name": feature_table},
            ).scalar()
        )

    if feature_table_exists:
        feature_join_sql = f"""
        LEFT JOIN {_fq(feature_db, feature_table)} f
          ON f.product_id = p.product_key
        """
        feature_select_sql = """
          f.brand AS feature_brand,
          f.product_name AS feature_product_name,
          f.primary_meat_source_count AS feature_primary_meat_source_count,
          f.secondary_meat_source_count AS feature_secondary_meat_source_count,
          f.primary_meat_source_type AS feature_primary_meat_source_type,
          f.secondary_meat_source_type AS feature_secondary_meat_source_type,
          f.crude_protein_pct AS feature_crude_protein_pct,
        """
    else:
        print(
            f"feature table {feature_db}.{feature_table} not found; "
            "using source labels and guarantee tables only"
        )
        feature_join_sql = ""
        feature_select_sql = """
          NULL AS feature_brand,
          NULL AS feature_product_name,
          NULL AS feature_primary_meat_source_count,
          NULL AS feature_secondary_meat_source_count,
          NULL AS feature_primary_meat_source_type,
          NULL AS feature_secondary_meat_source_type,
          NULL AS feature_crude_protein_pct,
        """

    sql = f"""
    WITH ranked_guarantee AS (
      SELECT
        g.source_id,
        g.product_id,
        g.metric_name,
        g.metric_value,
        g.metric_unit,
        ROW_NUMBER() OVER (
          PARTITION BY g.source_id
          ORDER BY
            CASE WHEN g.basis = '干物质' THEN 1 ELSE 0 END DESC,
            g.id DESC
        ) AS rn
      FROM {product_guarantee_fq} g
      WHERE g.metric_name = '粗蛋白'
    )
    SELECT
      p.source_id,
      p.product_key,
      p.brand,
      p.product_name,
      p.animal_sources,
      p.protein_source_details,
      p.primary_meat_source_species,
      p.secondary_meat_source_species,
      p.primary_meat_source_type,
      p.secondary_meat_source_type,
      p.protein_source_origin,
      p.plant_protein_labels,
      {feature_select_sql}
      i.id AS product_info_id,
      rg.product_id AS guarantee_product_id,
      rg.metric_name AS guarantee_metric_name,
      rg.metric_value AS guarantee_metric_value,
      rg.metric_unit AS guarantee_metric_unit
    FROM {source_fq} p
    INNER JOIN {parsed_fq} parsed
      ON parsed.id = p.parsed_row_id
     AND parsed.ingredient_composition IS NOT NULL
     AND TRIM(parsed.ingredient_composition) <> ''
    {feature_join_sql}
    LEFT JOIN {product_info_fq} i
      ON i.source_id = p.source_id
    LEFT JOIN ranked_guarantee rg
      ON rg.source_id = p.source_id
     AND rg.rn = 1
    ORDER BY p.source_id ASC
    """
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql)).mappings().all()]


def _load_labeling_helpers():
    if not (CSV_LABELING_PROJECT / "src").exists():
        raise FileNotFoundError(f"missing csv labeling project: {CSV_LABELING_PROJECT}")
    project_text = str(CSV_LABELING_PROJECT)
    if project_text not in sys.path:
        sys.path.insert(0, project_text)

    try:
        from src.parse_catfood_ingredient_types import (  # type: ignore
            _build_product_key,
            _build_protein_detail_rows,
            _build_protein_label_row,
            _classify_ingredient_composition,
            _composition_cache_key,
            _resolve_openai_cfg,
        )
    except Exception as exc:  # pragma: no cover - depends on sibling project imports
        raise RuntimeError(
            "failed to import protein label helpers from csv_mysql_labeling_project"
        ) from exc

    return {
        "build_product_key": _build_product_key,
        "build_protein_detail_rows": _build_protein_detail_rows,
        "build_protein_label_row": _build_protein_label_row,
        "classify_ingredient_composition": _classify_ingredient_composition,
        "composition_cache_key": _composition_cache_key,
        "resolve_openai_cfg": _resolve_openai_cfg,
    }


def load_rows_from_parsed_incremental(
    engine: Engine,
    *,
    parsed_db: str,
    parsed_table: str,
    feature_db: str,
    feature_table: str,
    guarantee_db: str,
    product_info_table: str,
    product_guarantee_table: str,
    target_db: str,
    target_table: str,
    limit: int,
    concurrency: int,
) -> list[dict[str, Any]]:
    helpers = _load_labeling_helpers()
    parsed_fq = _fq(parsed_db, parsed_table)
    target_fq = _fq(target_db, target_table)
    product_info_fq = _fq(guarantee_db, product_info_table)
    product_guarantee_fq = _fq(guarantee_db, product_guarantee_table)
    target_exists = _table_exists(engine, target_db, target_table)
    feature_table_exists = _table_exists(engine, feature_db, feature_table)

    target_filter = (
        f"""
      AND (
        NOT EXISTS (
          SELECT 1
          FROM {target_fq} t
          WHERE t.source_id = p.source_id
        )
        OR EXISTS (
          SELECT 1
          FROM {target_fq} t
          WHERE t.source_id = p.source_id
            AND (
              t.aggregated_at IS NULL
              OR p.updated_ts > t.aggregated_at
            )
        )
        OR EXISTS (
          SELECT 1
          FROM {target_fq} t
          INNER JOIN {product_guarantee_fq} g
            ON (
              g.source_id = p.source_id
              OR (
                p.merged_source_ids IS NOT NULL
                AND FIND_IN_SET(CAST(g.source_id AS CHAR), p.merged_source_ids) > 0
              )
            )
           AND g.metric_name = '粗蛋白'
          WHERE t.source_id = p.source_id
            AND (
              t.guarantee_crude_protein_value IS NULL
              OR t.aggregated_at IS NULL
              OR g.updated_at > t.aggregated_at
            )
        )
      )
        """
        if target_exists
        else ""
    )
    limit_sql = "LIMIT :limit_value" if limit > 0 else ""
    if feature_table_exists:
        feature_join_sql = f"""
        LEFT JOIN {_fq(feature_db, feature_table)} f
          ON f.product_id = :product_key
        """
        feature_select_sql = """
          f.brand AS feature_brand,
          f.product_name AS feature_product_name,
          f.primary_meat_source_count AS feature_primary_meat_source_count,
          f.secondary_meat_source_count AS feature_secondary_meat_source_count,
          f.primary_meat_source_type AS feature_primary_meat_source_type,
          f.secondary_meat_source_type AS feature_secondary_meat_source_type,
          f.crude_protein_pct AS feature_crude_protein_pct,
        """
    else:
        feature_join_sql = ""
        feature_select_sql = """
          NULL AS feature_brand,
          NULL AS feature_product_name,
          NULL AS feature_primary_meat_source_count,
          NULL AS feature_secondary_meat_source_count,
          NULL AS feature_primary_meat_source_type,
          NULL AS feature_secondary_meat_source_type,
          NULL AS feature_crude_protein_pct,
        """

    parsed_sql = f"""
    SELECT p.id, p.source_id, p.image_name, p.image_path, p.brand, p.product_name, p.ingredient_composition, p.merged_source_ids
    FROM {parsed_fq} p
    WHERE p.source_id IS NOT NULL
      AND p.ingredient_composition IS NOT NULL
      AND TRIM(p.ingredient_composition) <> ''
      {target_filter}
    ORDER BY p.id ASC
    {limit_sql}
    """
    params = {"limit_value": int(limit)} if limit > 0 else {}
    with engine.connect() as conn:
        parsed_rows = [dict(row) for row in conn.execute(text(parsed_sql), params).mappings().all()]

    if not parsed_rows:
        return []

    grouped_rows: dict[str, dict[str, Any]] = {}
    for row in parsed_rows:
        ingredient_composition = str(row.get("ingredient_composition") or "").strip()
        cache_key = helpers["composition_cache_key"](ingredient_composition)
        grouped_rows.setdefault(
            cache_key,
            {"ingredient_composition": ingredient_composition, "rows": []},
        )["rows"].append(row)

    batch_id = uuid.uuid4().hex[:12]
    resolved_openai_cfg = helpers["resolve_openai_cfg"](None)
    label_rows: list[dict[str, Any]] = []
    max_workers = min(max(1, int(concurrency)), len(grouped_rows))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                helpers["classify_ingredient_composition"],
                str(group["ingredient_composition"] or ""),
                resolved_openai_cfg,
            ): cache_key
            for cache_key, group in grouped_rows.items()
        }
        for future in concurrent.futures.as_completed(future_map):
            cache_key = future_map[future]
            classified = future.result()
            group = grouped_rows[cache_key]
            ingredient_composition = str(group["ingredient_composition"] or "")
            for row in group["rows"]:
                protein_feature_items = helpers["build_protein_detail_rows"](
                    row,
                    ingredient_composition,
                    classified,
                    batch_id,
                )
                label_row = helpers["build_protein_label_row"](
                    row=row,
                    ingredient_composition=ingredient_composition,
                    classified=classified,
                    batch_id=batch_id,
                    protein_feature_items=protein_feature_items,
                )
                label_row["merged_source_ids"] = row.get("merged_source_ids")
                label_rows.append(label_row)

    enrich_sql = f"""
    WITH ranked_guarantee AS (
      SELECT
        g.source_id,
        g.product_id,
        g.metric_name,
        g.metric_value,
        g.metric_unit,
        ROW_NUMBER() OVER (
          PARTITION BY g.source_id
          ORDER BY
            CASE WHEN g.basis = '干物质' THEN 1 ELSE 0 END DESC,
            g.id DESC
        ) AS rn
      FROM {product_guarantee_fq} g
      WHERE g.metric_name = '粗蛋白'
    ),
    ranked_info AS (
      SELECT
        i.id,
        i.source_id,
        ROW_NUMBER() OVER (
          ORDER BY
            CASE WHEN rg.product_id IS NOT NULL THEN 1 ELSE 0 END DESC,
            CASE WHEN i.source_id = :source_id THEN 1 ELSE 0 END DESC,
            i.id DESC
        ) AS rn
      FROM {product_info_fq} i
      LEFT JOIN ranked_guarantee rg
        ON rg.source_id = i.source_id
       AND rg.rn = 1
      WHERE i.source_id = :source_id
         OR (
           :merged_source_ids IS NOT NULL
           AND FIND_IN_SET(CAST(i.source_id AS CHAR), :merged_source_ids) > 0
         )
    )
    SELECT
      {feature_select_sql}
      ri.id AS product_info_id,
      rg.product_id AS guarantee_product_id,
      rg.metric_name AS guarantee_metric_name,
      rg.metric_value AS guarantee_metric_value,
      rg.metric_unit AS guarantee_metric_unit
    FROM (SELECT :source_id AS source_id, :product_key AS product_key, :merged_source_ids AS merged_source_ids) p
    {feature_join_sql}
    LEFT JOIN ranked_info ri
      ON ri.rn = 1
    LEFT JOIN ranked_guarantee rg
      ON (
        rg.source_id = p.source_id
        OR (
          p.merged_source_ids IS NOT NULL
          AND FIND_IN_SET(CAST(rg.source_id AS CHAR), p.merged_source_ids) > 0
        )
      )
     AND rg.rn = 1
    ORDER BY
      CASE WHEN rg.product_id IS NOT NULL THEN 1 ELSE 0 END DESC,
      CASE WHEN rg.source_id = p.source_id THEN 1 ELSE 0 END DESC
    LIMIT 1
    """

    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for label_row in label_rows:
            product_key = label_row.get("product_key") or helpers["build_product_key"](
                label_row.get("brand"),
                label_row.get("product_name"),
                label_row.get("source_id"),
            )
            enriched = dict(
                conn.execute(
                    text(enrich_sql),
                    {
                        "source_id": label_row["source_id"],
                        "product_key": product_key,
                        "merged_source_ids": _clean_text(label_row.get("merged_source_ids")),
                    },
                ).mappings().first()
                or {}
            )
            out.append({**label_row, **enriched})
    return out


def transform_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        level1_categories, level2_sources = _classify_animal_sources(
            row.get("animal_sources"),
            row.get("protein_source_details"),
        )
        primary_species = _clean_text(row.get("primary_meat_source_species"))
        secondary_species = _clean_text(row.get("secondary_meat_source_species"))
        primary_type = _clean_text(row.get("primary_meat_source_type")) or _clean_text(row.get("feature_primary_meat_source_type"))
        secondary_type = _clean_text(row.get("secondary_meat_source_type")) or _clean_text(row.get("feature_secondary_meat_source_type"))

        primary_count = row.get("feature_primary_meat_source_count")
        if primary_count is None:
            primary_count = _count_multi_values(primary_species)

        secondary_count = row.get("feature_secondary_meat_source_count")
        if secondary_count is None:
            secondary_count = _count_multi_values(secondary_species)

        guarantee_value = row.get("guarantee_metric_value")
        guarantee_metric_name = _clean_text(row.get("guarantee_metric_name"))
        guarantee_metric_unit = _clean_text(row.get("guarantee_metric_unit"))
        if guarantee_value is None and row.get("feature_crude_protein_pct") is not None:
            guarantee_value = row.get("feature_crude_protein_pct")
            guarantee_metric_name = "粗蛋白"
            guarantee_metric_unit = "%"

        brand_name = correct_brand(
            _clean_text(row.get("brand")) or _clean_text(row.get("feature_brand")),
            _clean_text(row.get("product_name")) or _clean_text(row.get("feature_product_name")),
            row.get("image_name"),
            row.get("image_path"),
        )
        product_name = _clean_text(row.get("product_name")) or _clean_text(row.get("feature_product_name"))

        out.append(
            {
                "source_id": int(row["source_id"]),
                "product_key": build_corrected_product_key(brand_name, product_name),
                "guarantee_product_id": row.get("product_info_id") or row.get("guarantee_product_id"),
                "brand_name": brand_name,
                "product_name": product_name,
                "animal_sources": _clean_text(row.get("animal_sources")),
                "animal_source_level1_categories": level1_categories,
                "animal_source_level2_sources": level2_sources,
                "protein_source_details": _clean_text(row.get("protein_source_details")),
                "primary_meat_source_species": primary_species,
                "secondary_meat_source_species": secondary_species,
                "primary_meat_source_type": primary_type,
                "secondary_meat_source_type": secondary_type,
                "primary_meat_source_count": primary_count,
                "secondary_meat_source_count": secondary_count,
                "protein_source_origin": _clean_text(row.get("protein_source_origin")),
                "plant_protein_labels": _clean_text(row.get("plant_protein_labels")),
                "guarantee_crude_protein_metric_name": guarantee_metric_name,
                "guarantee_crude_protein_value": guarantee_value,
                "guarantee_crude_protein_unit": guarantee_metric_unit,
            }
        )
    return out


def write_rows(
    engine: Engine,
    *,
    target_db: str,
    target_table: str,
    rows: list[dict[str, Any]],
    keep_backup: bool,
    replace_existing: bool = True,
) -> tuple[str, Optional[str]]:
    batch_id = uuid.uuid4().hex[:12]
    target_fq = _fq(target_db, target_table)

    insert_stmt = text(
        f"""
        INSERT INTO {target_fq} (
          source_id,
          product_key,
          guarantee_product_id,
          brand_name,
          product_name,
          animal_sources,
          animal_source_level1_categories,
          animal_source_level2_sources,
          protein_source_details,
          primary_meat_source_species,
          secondary_meat_source_species,
          primary_meat_source_type,
          secondary_meat_source_type,
          primary_meat_source_count,
          secondary_meat_source_count,
          protein_source_origin,
          plant_protein_labels,
          guarantee_crude_protein_metric_name,
          guarantee_crude_protein_value,
          guarantee_crude_protein_unit
        ) VALUES (
          :source_id,
          :product_key,
          :guarantee_product_id,
          :brand_name,
          :product_name,
          :animal_sources,
          :animal_source_level1_categories,
          :animal_source_level2_sources,
          :protein_source_details,
          :primary_meat_source_species,
          :secondary_meat_source_species,
          :primary_meat_source_type,
          :secondary_meat_source_type,
          :primary_meat_source_count,
          :secondary_meat_source_count,
          :protein_source_origin,
          :plant_protein_labels,
          :guarantee_crude_protein_metric_name,
          :guarantee_crude_protein_value,
          :guarantee_crude_protein_unit
        )
        ON DUPLICATE KEY UPDATE
          product_key = VALUES(product_key),
          guarantee_product_id = VALUES(guarantee_product_id),
          brand_name = VALUES(brand_name),
          product_name = VALUES(product_name),
          animal_sources = VALUES(animal_sources),
          animal_source_level1_categories = VALUES(animal_source_level1_categories),
          animal_source_level2_sources = VALUES(animal_source_level2_sources),
          protein_source_details = VALUES(protein_source_details),
          primary_meat_source_species = VALUES(primary_meat_source_species),
          secondary_meat_source_species = VALUES(secondary_meat_source_species),
          primary_meat_source_type = VALUES(primary_meat_source_type),
          secondary_meat_source_type = VALUES(secondary_meat_source_type),
          primary_meat_source_count = VALUES(primary_meat_source_count),
          secondary_meat_source_count = VALUES(secondary_meat_source_count),
          protein_source_origin = VALUES(protein_source_origin),
          plant_protein_labels = VALUES(plant_protein_labels),
          guarantee_crude_protein_metric_name = VALUES(guarantee_crude_protein_metric_name),
          guarantee_crude_protein_value = VALUES(guarantee_crude_protein_value),
          guarantee_crude_protein_unit = VALUES(guarantee_crude_protein_unit),
          aggregated_at = CURRENT_TIMESTAMP
        """
    )

    with engine.begin() as conn:
        conn.execute(text(_target_table_ddl(target_table).replace(f"`{target_table}`", f"{target_fq}", 1)))
        if keep_backup:
            backup_table = _limit_table_name(target_table, "bak", batch_id)
            backup_fq = _fq(target_db, backup_table)
            conn.execute(text(f"DROP TABLE IF EXISTS {backup_fq}"))
            conn.execute(text(f"CREATE TABLE {backup_fq} LIKE {target_fq}"))
            conn.execute(text(f"INSERT INTO {backup_fq} SELECT * FROM {target_fq}"))
        if replace_existing:
            conn.execute(text(f"DELETE FROM {target_fq}"))
        if rows:
            conn.execute(insert_stmt, rows)

    return batch_id, backup_table if keep_backup else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "row_count": len(rows),
        "with_product_info": sum(1 for row in rows if row.get("guarantee_product_id") is not None),
        "with_guarantee_value": sum(1 for row in rows if row.get("guarantee_crude_protein_value") is not None),
        "with_animal_sources": sum(1 for row in rows if row.get("animal_sources")),
        "with_level1_categories": sum(1 for row in rows if row.get("animal_source_level1_categories")),
        "with_level2_sources": sum(1 for row in rows if row.get("animal_source_level2_sources")),
        "with_plant_protein_labels": sum(1 for row in rows if row.get("plant_protein_labels")),
    }


def print_preview(rows: list[dict[str, Any]], limit: int) -> None:
    for row in rows[:limit]:
        print(
            json.dumps(
                {
                    "source_id": row["source_id"],
                    "product_key": row["product_key"],
                    "brand_name": row["brand_name"],
                    "product_name": row["product_name"],
                    "animal_sources": row["animal_sources"],
                    "animal_source_level1_categories": row["animal_source_level1_categories"],
                    "animal_source_level2_sources": row["animal_source_level2_sources"],
                    "primary_meat_source_species": row["primary_meat_source_species"],
                    "secondary_meat_source_species": row["secondary_meat_source_species"],
                    "primary_meat_source_count": row["primary_meat_source_count"],
                    "secondary_meat_source_count": row["secondary_meat_source_count"],
                    "protein_source_origin": row["protein_source_origin"],
                    "guarantee_crude_protein_value": str(row["guarantee_crude_protein_value"]) if row["guarantee_crude_protein_value"] is not None else None,
                },
                ensure_ascii=False,
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build protein_feature_platform.protein_source_aggregate from protein labels or parsed OCR rows."
    )
    parser.add_argument("--dsn", help="full SQLAlchemy DSN")
    parser.add_argument("--host", default=None, help="MySQL host")
    parser.add_argument("--port", default=None, help="MySQL port")
    parser.add_argument("--user", default=None, help="MySQL user")
    parser.add_argument("--password", default=None, help="MySQL password")
    parser.add_argument("--charset", default="utf8mb4", help="MySQL charset")
    parser.add_argument("--source-db", default="csv_labeling", help="database containing catfood_feature_protein_labels")
    parser.add_argument("--source-table", default="catfood_feature_protein_labels", help="source protein detail table")
    parser.add_argument(
        "--input-mode",
        choices=["direct", "legacy-label-table"],
        default="direct",
        help="direct reads new rows from catfood_ingredient_ocr_parsed; legacy-label-table reads catfood_feature_protein_labels",
    )
    parser.add_argument("--parsed-db", default="csv_labeling", help="database containing catfood_ingredient_ocr_parsed")
    parser.add_argument("--parsed-table", default="catfood_ingredient_ocr_parsed", help="current parsed OCR table used to filter stale protein labels")
    parser.add_argument("--feature-db", default="protein_feature_platform", help="database containing protein_source_feature")
    parser.add_argument("--feature-table", default="protein_source_feature", help="feature summary table")
    parser.add_argument("--guarantee-db", default="csv_labeling", help="database containing product_info/product_guarantee")
    parser.add_argument("--product-info-table", default="product_info", help="product info table")
    parser.add_argument("--product-guarantee-table", default="product_guarantee", help="product guarantee table")
    parser.add_argument("--target-db", default="protein_feature_platform", help="target database")
    parser.add_argument("--target-table", default="protein_source_aggregate", help="target aggregate table")
    parser.add_argument("--limit", type=int, default=0, help="max parsed OCR rows to process in direct mode, 0 means all new rows")
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("CATFOOD_PROTEIN_LABEL_CONCURRENCY", "4")), help="parallel LLM requests in direct mode")
    parser.add_argument("--preview-limit", type=int, default=10, help="preview rows in dry-run mode")
    parser.add_argument("--dry-run", action="store_true", help="print summary and preview without writing")
    parser.add_argument("--keep-backup", action="store_true", help="keep previous target table after swap")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_db = _safe_name(args.source_db, "source db")
    source_table = _safe_name(args.source_table, "source table")
    parsed_db = _safe_name(args.parsed_db, "parsed db")
    parsed_table = _safe_name(args.parsed_table, "parsed table")
    feature_db = _safe_name(args.feature_db, "feature db")
    feature_table = _safe_name(args.feature_table, "feature table")
    guarantee_db = _safe_name(args.guarantee_db, "guarantee db")
    product_info_table = _safe_name(args.product_info_table, "product info table")
    product_guarantee_table = _safe_name(args.product_guarantee_table, "product guarantee table")
    target_db = _safe_name(args.target_db, "target db")
    target_table = _safe_name(args.target_table, "target table")

    engine = make_engine(args)
    if args.input_mode == "direct":
        raw_rows = load_rows_from_parsed_incremental(
            engine,
            parsed_db=parsed_db,
            parsed_table=parsed_table,
            feature_db=feature_db,
            feature_table=feature_table,
            guarantee_db=guarantee_db,
            product_info_table=product_info_table,
            product_guarantee_table=product_guarantee_table,
            target_db=target_db,
            target_table=target_table,
            limit=max(0, int(args.limit)),
            concurrency=max(1, int(args.concurrency)),
        )
    else:
        raw_rows = load_rows(
            engine,
            source_db=source_db,
            source_table=source_table,
            parsed_db=parsed_db,
            parsed_table=parsed_table,
            feature_db=feature_db,
            feature_table=feature_table,
            guarantee_db=guarantee_db,
            product_info_table=product_info_table,
            product_guarantee_table=product_guarantee_table,
        )
    rows = transform_rows(raw_rows)
    summary = summarize(rows)
    summary.update(
        {
            "input_mode": args.input_mode,
            "source": f"{parsed_db}.{parsed_table}" if args.input_mode == "direct" else f"{source_db}.{source_table}",
            "parsed_filter": f"{parsed_db}.{parsed_table}",
            "feature": f"{feature_db}.{feature_table}",
            "target": f"{target_db}.{target_table}",
            "write_mode": "append_new_source_ids" if args.input_mode == "direct" else "replace_all",
            "dry_run": bool(args.dry_run),
        }
    )
    print(json.dumps(summary, ensure_ascii=False))

    if args.dry_run:
        print_preview(rows, max(1, int(args.preview_limit)))
        return 0

    batch_id, backup_table = write_rows(
        engine,
        target_db=target_db,
        target_table=target_table,
        rows=rows,
        keep_backup=bool(args.keep_backup),
        replace_existing=args.input_mode != "direct",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "target": f"{target_db}.{target_table}",
                "written_rows": len(rows),
                "batch_id": batch_id,
                "backup_table": backup_table,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
