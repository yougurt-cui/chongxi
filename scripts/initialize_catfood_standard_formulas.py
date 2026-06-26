#!/usr/bin/env python3
"""Initialize standard formula versions from approved standard-product OCR lineage."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402
from services.catfood_standardization_service import (  # noqa: E402
    FORMULA_TABLE,
    MAPPING_TABLE,
    PRODUCT_CANDIDATE_TABLE,
    PRODUCT_TABLE,
    _json_list,
    _load_nutrition_signature,
    normalize_ingredients,
    nutrition_similarity,
    ordered_ingredient_similarity,
)


@dataclass
class FormulaSource:
    source_id: int
    parsed_row_id: int
    file_sha256: str
    raw_brand_name: str
    raw_product_name: str
    raw_ingredient_composition: str
    normalized_composition: str
    ingredients: list[str]
    fingerprint: str
    nutrition: dict[str, dict[str, Any]]


@dataclass
class FormulaCluster:
    sources: list[FormulaSource] = field(default_factory=list)

    @property
    def canonical(self) -> FormulaSource:
        return max(self.sources, key=lambda item: (item.source_id, item.parsed_row_id))


def should_merge_formula_sources(left: FormulaSource, right: FormulaSource) -> bool:
    if left.fingerprint == right.fingerprint:
        return True
    ingredient_evidence = ordered_ingredient_similarity(left.ingredients, right.ingredients)
    nutrition_evidence = nutrition_similarity(left.nutrition, right.nutrition)
    return bool(
        ingredient_evidence["score"] >= 0.96
        and ingredient_evidence["top5_exact_ratio"] >= 0.8
        and nutrition_evidence["comparable_count"] >= 4
        and nutrition_evidence["score"] >= 0.95
    )


def cluster_formula_sources(sources: list[FormulaSource]) -> list[FormulaCluster]:
    clusters: list[FormulaCluster] = []
    for source in sorted(sources, key=lambda item: (item.source_id, item.parsed_row_id)):
        matching = [
            cluster
            for cluster in clusters
            if should_merge_formula_sources(source, cluster.canonical)
        ]
        if len(matching) == 1:
            matching[0].sources.append(source)
        else:
            clusters.append(FormulaCluster(sources=[source]))
    return clusters


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _load_products(cursor) -> list[dict[str, Any]]:
    cursor.execute(
        f"""
        SELECT
          p.product_id,
          p.brand_id,
          p.standard_product_name,
          c.source_ids_json
        FROM `{PRODUCT_TABLE}` p
        JOIN `{PRODUCT_CANDIDATE_TABLE}` c
          ON c.product_id = p.source_candidate_id
        WHERE p.active = 1
          AND c.review_status = 'approved'
        ORDER BY p.product_id
        """
    )
    return list(cursor.fetchall())


def _load_formula_sources(cursor, source_ids: list[int]) -> list[FormulaSource]:
    sources = []
    for source_id in source_ids:
        cursor.execute(
            """
            SELECT
              id AS parsed_row_id,
              source_id,
              file_sha256,
              brand,
              product_name,
              ingredient_composition
            FROM catfood_ingredient_ocr_parsed
            WHERE source_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (source_id,),
        )
        row = cursor.fetchone()
        if not row:
            continue
        normalized, ingredients, fingerprint = normalize_ingredients(
            row.get("ingredient_composition")
        )
        if not ingredients:
            continue
        sources.append(
            FormulaSource(
                source_id=int(row["source_id"]),
                parsed_row_id=int(row["parsed_row_id"]),
                file_sha256=str(row.get("file_sha256") or ""),
                raw_brand_name=str(row.get("brand") or ""),
                raw_product_name=str(row.get("product_name") or ""),
                raw_ingredient_composition=str(row.get("ingredient_composition") or ""),
                normalized_composition=normalized,
                ingredients=ingredients,
                fingerprint=fingerprint,
                nutrition=_load_nutrition_signature(cursor, int(row["source_id"])),
            )
        )
    return sources


def _upsert_formula(
    cursor,
    *,
    product_id: int,
    version: int,
    cluster: FormulaCluster,
) -> int:
    canonical = cluster.canonical
    cursor.execute(
        f"""
        INSERT INTO `{FORMULA_TABLE}`(
          product_id, formula_version, raw_ingredient_example,
          normalized_ingredient_composition, normalized_ingredients_json,
          ingredient_fingerprint, nutrition_json, is_current, status
        ) VALUES(%s, %s, %s, %s, %s, %s, %s, 0, 'active')
        ON DUPLICATE KEY UPDATE
          raw_ingredient_example = VALUES(raw_ingredient_example),
          normalized_ingredient_composition = VALUES(normalized_ingredient_composition),
          normalized_ingredients_json = VALUES(normalized_ingredients_json),
          nutrition_json = VALUES(nutrition_json),
          status = 'active',
          formula_id = LAST_INSERT_ID(formula_id)
        """,
        (
            product_id,
            version,
            canonical.raw_ingredient_composition,
            canonical.normalized_composition,
            json.dumps(canonical.ingredients, ensure_ascii=False),
            canonical.fingerprint,
            json.dumps(canonical.nutrition, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def _upsert_mapping(
    cursor,
    *,
    product: dict[str, Any],
    formula_id: int,
    source: FormulaSource,
) -> None:
    cursor.execute(
        f"""
        INSERT INTO `{MAPPING_TABLE}`(
          source_id, parsed_row_id, file_sha256,
          raw_brand_name, raw_product_name, raw_ingredient_composition,
          brand_id, product_id, formula_id,
          brand_status, product_status, formula_status, overall_status,
          brand_confidence, product_confidence, formula_confidence,
          match_evidence_json
        ) VALUES(
          %s, %s, %s,
          %s, %s, %s,
          %s, %s, %s,
          'matched', 'matched', 'matched', 'matched',
          1.0, 1.0, 1.0,
          JSON_OBJECT('formula_initialization', true)
        )
        ON DUPLICATE KEY UPDATE
          parsed_row_id = VALUES(parsed_row_id),
          file_sha256 = VALUES(file_sha256),
          raw_brand_name = VALUES(raw_brand_name),
          raw_product_name = VALUES(raw_product_name),
          raw_ingredient_composition = VALUES(raw_ingredient_composition),
          brand_id = VALUES(brand_id),
          product_id = VALUES(product_id),
          formula_id = VALUES(formula_id),
          brand_status = 'matched',
          product_status = 'matched',
          formula_status = 'matched',
          overall_status = 'matched',
          brand_confidence = 1.0,
          product_confidence = 1.0,
          formula_confidence = 1.0,
          match_evidence_json = JSON_SET(
            COALESCE(match_evidence_json, JSON_OBJECT()),
            '$.formula_initialization', true
          )
        """,
        (
            source.source_id,
            source.parsed_row_id,
            source.file_sha256 or None,
            source.raw_brand_name or None,
            source.raw_product_name or None,
            source.raw_ingredient_composition,
            product["brand_id"],
            product["product_id"],
            formula_id,
        ),
    )


def initialize_formulas(*, apply: bool) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "products_scanned": 0,
        "products_with_formula_sources": 0,
        "products_without_formula_sources": [],
        "formula_versions": 0,
        "source_mappings": 0,
        "multi_version_products": [],
        "applied": apply,
    }
    with _connect() as conn:
        with conn.cursor() as cursor:
            products = _load_products(cursor)
            summary["products_scanned"] = len(products)
            for product in products:
                source_ids = [
                    int(value)
                    for value in _json_list(product.get("source_ids_json"))
                    if str(value).isdigit()
                ]
                sources = _load_formula_sources(cursor, source_ids)
                if not sources:
                    summary["products_without_formula_sources"].append(
                        {
                            "product_id": int(product["product_id"]),
                            "standard_product_name": product["standard_product_name"],
                            "source_ids": source_ids,
                        }
                    )
                    continue
                summary["products_with_formula_sources"] += 1
                clusters = cluster_formula_sources(sources)
                summary["formula_versions"] += len(clusters)
                summary["source_mappings"] += len(sources)
                if len(clusters) > 1:
                    summary["multi_version_products"].append(
                        {
                            "product_id": int(product["product_id"]),
                            "standard_product_name": product["standard_product_name"],
                            "versions": len(clusters),
                            "source_groups": [
                                [source.source_id for source in cluster.sources]
                                for cluster in clusters
                            ],
                        }
                    )
                if not apply:
                    continue

                cursor.execute(
                    f"""
                    SELECT ingredient_fingerprint, formula_version
                    FROM `{FORMULA_TABLE}`
                    WHERE product_id = %s
                    """,
                    (product["product_id"],),
                )
                existing_versions = {
                    row["ingredient_fingerprint"]: int(row["formula_version"])
                    for row in cursor.fetchall()
                }
                next_version = max(existing_versions.values(), default=0) + 1
                written: list[tuple[int, FormulaCluster]] = []
                for cluster in clusters:
                    fingerprint = cluster.canonical.fingerprint
                    version = existing_versions.get(fingerprint)
                    if version is None:
                        version = next_version
                        next_version += 1
                    formula_id = _upsert_formula(
                        cursor,
                        product_id=int(product["product_id"]),
                        version=version,
                        cluster=cluster,
                    )
                    written.append((formula_id, cluster))
                    for source in cluster.sources:
                        _upsert_mapping(
                            cursor,
                            product=product,
                            formula_id=formula_id,
                            source=source,
                        )
                current_formula_id, _ = max(
                    written,
                    key=lambda item: item[1].canonical.source_id,
                )
                cursor.execute(
                    f"UPDATE `{FORMULA_TABLE}` SET is_current = (formula_id = %s) WHERE product_id = %s",
                    (current_formula_id, product["product_id"]),
                )
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write formula versions and OCR mappings; default is dry-run",
    )
    args = parser.parse_args()
    print(json.dumps(initialize_formulas(apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
