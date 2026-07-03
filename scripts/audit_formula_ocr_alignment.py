#!/usr/bin/env python3
"""Compare standard formulas with parsed OCR rows by normalized ingredients."""

from __future__ import annotations

import json
import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402
from services.catfood_standardization_service import (  # noqa: E402
    normalize_ingredients,
    normalize_name,
    ordered_ingredient_similarity,
)


def _clean(value) -> str:
    return str(value or "").strip()


def audit() -> dict:
    cfg = get_mysql_config()
    with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT f.formula_id, f.normalized_ingredient_composition,
                       f.ingredient_fingerprint, p.product_id,
                       p.standard_product_name, p.display_name,
                       b.brand_id, b.standard_brand_name
                FROM catfood_standard_formula f
                JOIN catfood_standard_product p ON p.product_id=f.product_id
                JOIN catfood_standard_brand b ON b.brand_id=p.brand_id
                WHERE f.status='active' AND f.is_current=1 AND p.active=1 AND b.active=1
                ORDER BY f.formula_id
                """
            )
            formulas = list(cursor.fetchall())
            cursor.execute(
                """
                SELECT id, source_id, brand, product_name, ingredient_composition,
                       image_name, file_sha256
                FROM catfood_ingredient_ocr_parsed
                WHERE ingredient_composition IS NOT NULL
                  AND TRIM(ingredient_composition)<>''
                ORDER BY id
                """
            )
            parsed_rows = list(cursor.fetchall())
            cursor.execute("SELECT brand_id, alias_name FROM catfood_standard_brand_alias WHERE active=1")
            brand_alias_rows = list(cursor.fetchall())
            cursor.execute("SELECT product_id, alias_name FROM catfood_standard_product_alias WHERE active=1")
            product_alias_rows = list(cursor.fetchall())

    brand_names: dict[int, set[str]] = defaultdict(set)
    product_names: dict[int, set[str]] = defaultdict(set)
    for row in formulas:
        brand_names[int(row["brand_id"])].add(normalize_name(row["standard_brand_name"]))
        product_names[int(row["product_id"])].update(
            normalize_name(value) for value in (row["standard_product_name"], row["display_name"]) if _clean(value)
        )
    for row in brand_alias_rows:
        brand_names[int(row["brand_id"])].add(normalize_name(row["alias_name"]))
    for row in product_alias_rows:
        product_names[int(row["product_id"])].add(normalize_name(row["alias_name"]))

    parsed_by_fingerprint: dict[str, list[dict]] = defaultdict(list)
    normalized_parsed = []
    for row in parsed_rows:
        _, ingredients, fingerprint = normalize_ingredients(row["ingredient_composition"])
        item = {**row, "ingredients": ingredients, "fingerprint": fingerprint}
        normalized_parsed.append(item)
        if ingredients:
            parsed_by_fingerprint[fingerprint].append(item)

    results = []
    for formula in formulas:
        _, formula_items, calculated_fp = normalize_ingredients(formula["normalized_ingredient_composition"])
        formula_fp = _clean(formula["ingredient_fingerprint"]) or calculated_fp
        candidates = parsed_by_fingerprint.get(formula_fp, [])
        match_type = "exact"
        if not candidates:
            ranked = []
            for parsed in normalized_parsed:
                evidence = ordered_ingredient_similarity(formula_items, parsed["ingredients"])
                if evidence["score"] >= 0.75:
                    ranked.append((evidence["score"], evidence["top5_exact_ratio"], parsed, evidence))
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            candidates = [item[2] for item in ranked[:1]]
            match_type = "similar" if candidates else "unmatched"

        selected = None
        selected_evidence = None
        if candidates:
            scored_candidates = []
            for parsed in candidates:
                evidence = ordered_ingredient_similarity(formula_items, parsed["ingredients"])
                raw_brand = normalize_name(parsed["brand"])
                raw_product = normalize_name(parsed["product_name"])
                brand_match = bool(raw_brand and raw_brand in brand_names[int(formula["brand_id"])])
                product_match = bool(raw_product and raw_product in product_names[int(formula["product_id"])])
                identity_score = int(brand_match) + int(product_match)
                scored_candidates.append((identity_score, evidence["score"], parsed, evidence, brand_match, product_match))
            scored_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            _, _, selected, selected_evidence, brand_match, product_match = scored_candidates[0]
        else:
            brand_match = product_match = False

        results.append(
            {
                "formula_id": formula["formula_id"],
                "standard_brand": formula["standard_brand_name"],
                "standard_product": formula["standard_product_name"],
                "match_type": match_type,
                "ingredient_score": selected_evidence["score"] if selected_evidence else 0,
                "top5_exact_ratio": selected_evidence["top5_exact_ratio"] if selected_evidence else 0,
                "parsed_id": selected.get("id") if selected else None,
                "source_id": selected.get("source_id") if selected else None,
                "ocr_brand": selected.get("brand") if selected else None,
                "ocr_product": selected.get("product_name") if selected else None,
                "brand_match": brand_match,
                "product_match": product_match,
            }
        )

    summary = Counter(row["match_type"] for row in results)
    matched = [row for row in results if row["match_type"] != "unmatched"]
    return {
        "summary": {
            "formula_count": len(formulas),
            "parsed_count": len(parsed_rows),
            "exact_ingredient_matches": summary["exact"],
            "similar_ingredient_matches": summary["similar"],
            "unmatched": summary["unmatched"],
            "brand_matches": sum(row["brand_match"] for row in matched),
            "product_matches": sum(row["product_match"] for row in matched),
            "brand_and_product_matches": sum(row["brand_match"] and row["product_match"] for row in matched),
        },
        "identity_mismatches": [
            row for row in matched if not row["brand_match"] or not row["product_match"]
        ],
        "unmatched_formulas": [row for row in results if row["match_type"] == "unmatched"],
        "matches": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", action="store_true", help="print summary and mismatch samples only")
    args = parser.parse_args()
    result = audit()
    if args.summary:
        result = {
            "summary": result["summary"],
            "identity_mismatch_count": len(result["identity_mismatches"]),
            "identity_mismatch_samples": result["identity_mismatches"][:30],
            "unmatched_count": len(result["unmatched_formulas"]),
            "unmatched_samples": result["unmatched_formulas"][:30],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
