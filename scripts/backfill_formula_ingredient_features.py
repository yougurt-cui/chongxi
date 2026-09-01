#!/usr/bin/env python3
"""Backfill formula ingredient item/profile tables for selected formula ids."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pymysql
from sqlalchemy import create_engine


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "vendor" / "feature_score_pipeline" / "scripts"
for path in (BASE_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app_config import get_mysql_config  # noqa: E402
from scripts.initialize_ingredient_feature_tables import (  # noqa: E402
    FORMULA_FEATURE_PROFILE_TABLE,
    FORMULA_INGREDIENT_ITEM_TABLE,
    INGREDIENT_CANDIDATE_TABLE,
    INGREDIENT_FEATURE_RULE_TABLE,
    ensure_tables,
)
from vendor.feature_score_pipeline.scripts import rebuild_protein_source_aggregate as protein_aggregate  # noqa: E402
from services.fiber_science_materialization_service import build_science_payload  # noqa: E402
from services.fat_science_materialization_service import build_science_features  # noqa: E402


FORMULA_INPUT_TABLE = "catfood_formula_feature_input"
FAT_TARGET_TABLE = "protein_feature_platform.catfood_fat_material_features"
FIBER_TARGET_DB = "protein_feature_platform"
FIBER_TARGET_TABLE = "catfood_fiber_feature_json"


def _connect():
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _engine():
    cfg = get_mysql_config()
    url = (
        f"mysql+pymysql://{cfg['user']}:{cfg.get('password') or ''}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}?charset={cfg.get('charset', 'utf8mb4')}"
    )
    return create_engine(url, future=True)


def _json_array(value: Any) -> str:
    if not value:
        return json.dumps([], ensure_ascii=False)
    if isinstance(value, list):
        items = value
    else:
        items = protein_aggregate._split_source_tokens(value)
    return json.dumps(items, ensure_ascii=False)


def _strip_additives(value: Any) -> str:
    return protein_aggregate._strip_additive_section(value)


def _features_for_item(item: dict[str, Any]) -> dict[str, Any]:
    features: dict[str, Any] = dict(item.get("protein_rule_features") or {})
    if item.get("is_protein"):
        features["protein.is_protein"] = True
        if item.get("is_plant_protein"):
            features["protein.plant_protein"] = True
            if item.get("plant_protein_level"):
                features["protein.plant_protein_level"] = item["plant_protein_level"]
        if item.get("protein_form"):
            features["protein.form"] = item["protein_form"]
        if item.get("animal_source"):
            features["protein.animal_source"] = item["animal_source"]
    if item.get("protein_rule_ids"):
        features["protein.rule_ids"] = item["protein_rule_ids"]
    return features


def _is_structural_compound(raw_name: Any) -> bool:
    text = str(raw_name or "").strip()
    return bool(
        re.match(r"^矿物(?:质|群)", text)
        and re.search(r"[（(\[]", text)
        and re.search(r"[、,，]", text)
    )


def _domain_status_for_unmatched(items: list[dict[str, Any]]) -> dict[str, Any]:
    unmatched = [item for item in items if not item.get("standard_ingredient_id")]
    unmatched_payload = []
    domain_status = {
        "protein": "ready",
        "fat": "ready",
        "fiber": "ready",
        "starch": "ready",
    }
    blocking_domains: set[str] = set()
    warning_domains: set[str] = set()
    for item in unmatched:
        raw_name = str(item.get("raw_name") or "")
        likely_domains: list[str] = []
        if item.get("is_protein") or any(marker in raw_name for marker in ("肉", "鱼粉", "蛋白", "肝", "心", "蛋")):
            likely_domains.append("protein")
        if any(marker in raw_name for marker in ("油", "脂", "鱼油", "磷虾油")):
            likely_domains.extend(["fat", "skin"])
        if any(marker in raw_name for marker in ("纤维", "菊粉", "车前子", "甜菜粕", "果寡糖")):
            likely_domains.extend(["fiber", "gut"])
        if any(marker in raw_name for marker in ("淀粉", "薯", "米", "麦", "豆")) and "蛋白" not in raw_name:
            likely_domains.append("starch")
        if not likely_domains:
            likely_domains.append("unknown")
        severity = "blocking" if any(domain in {"protein", "fat", "fiber", "starch"} for domain in likely_domains) else "warning"
        for domain in likely_domains:
            if domain in domain_status:
                if severity == "blocking":
                    blocking_domains.add(domain)
                else:
                    warning_domains.add(domain)
        unmatched_payload.append(
            {
                "raw_name": raw_name,
                "likely_domains": sorted(set(likely_domains)),
                "severity": severity,
                "reason": "unmatched_standard_ingredient",
            }
        )
    for domain in blocking_domains:
        domain_status[domain] = "blocked"
    for domain in warning_domains - blocking_domains:
        domain_status[domain] = "ready_with_warnings"
    overall = "blocked" if blocking_domains else "ready_with_warnings" if warning_domains else "ready"
    return {
        "domain_status": domain_status,
        "overall_compare_status": overall,
        "blocking_domains": sorted(blocking_domains),
        "warning_domains": sorted(warning_domains - blocking_domains),
        "unmatched_ingredients": unmatched_payload,
    }


def _ensure_fiber_target(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FIBER_TARGET_DB}.{FIBER_TARGET_TABLE} (
            formula_id BIGINT UNSIGNED NOT NULL,
            raw_ingredient_text LONGTEXT,
            ingredient_feature_json JSON NOT NULL,
            starch_ingredients_json JSON NULL,
            profile_status VARCHAR(32) NULL,
            profile_version VARCHAR(32) NULL,
            source_fingerprint CHAR(64) NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (formula_id),
            KEY idx_profile_status (profile_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def _ensure_fat_target(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FAT_TARGET_TABLE} (
            formula_id BIGINT UNSIGNED NOT NULL,
            ingredient_composition LONGTEXT NULL,
            fat_sources TEXT NULL,
            fat_source_types VARCHAR(255) NULL,
            antioxidant_sources TEXT NULL,
            antioxidant_types VARCHAR(255) NULL,
            micronutrient_sources TEXT NULL,
            micronutrient_types VARCHAR(255) NULL,
            omega6_sources TEXT NULL,
            omega3_sources TEXT NULL,
            guarantee_crude_fat_value DECIMAL(18,2) NULL,
            guarantee_crude_fat_operator VARCHAR(10) NULL,
            guarantee_crude_fat_basis VARCHAR(50) NULL,
            profile_status VARCHAR(32) NULL,
            profile_version VARCHAR(32) NULL,
            source_fingerprint CHAR(64) NULL,
            needs_review TINYINT DEFAULT 0,
            review_reason VARCHAR(255) NULL,
            updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (formula_id),
            KEY idx_profile_status (profile_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def _load_science_profiles(cursor, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    standard_ids = sorted(
        {str(item["standard_ingredient_id"]) for item in items if item.get("standard_ingredient_id")}
    )
    if not standard_ids:
        return {}
    cursor.execute(
        "SELECT * FROM catfood_ingredient_science_profile WHERE standard_ingredient_id IN ("
        + ",".join(["%s"] * len(standard_ids))
        + ")",
        standard_ids,
    )
    return {str(row["standard_ingredient_id"]): row for row in cursor.fetchall()}


def _upsert_fat_feature(cursor, *, formula: dict[str, Any], ingredient_text: str, features: dict[str, Any]) -> None:
    cursor.execute(
        f"""
        INSERT INTO {FAT_TARGET_TABLE}(
          formula_id, ingredient_composition,
          fat_sources, fat_source_types, antioxidant_sources, antioxidant_types,
          micronutrient_sources, micronutrient_types, omega6_sources, omega3_sources,
          profile_status, profile_version, source_fingerprint, needs_review, review_reason
        ) VALUES(
          %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s, %s
        )
        ON DUPLICATE KEY UPDATE
          ingredient_composition = VALUES(ingredient_composition),
          fat_sources = VALUES(fat_sources),
          fat_source_types = VALUES(fat_source_types),
          antioxidant_sources = VALUES(antioxidant_sources),
          antioxidant_types = VALUES(antioxidant_types),
          micronutrient_sources = VALUES(micronutrient_sources),
          micronutrient_types = VALUES(micronutrient_types),
          omega6_sources = VALUES(omega6_sources),
          omega3_sources = VALUES(omega3_sources),
          profile_status = VALUES(profile_status),
          profile_version = VALUES(profile_version),
          source_fingerprint = VALUES(source_fingerprint),
          needs_review = VALUES(needs_review),
          review_reason = VALUES(review_reason)
        """,
        (
            int(formula["formula_id"]),
            ingredient_text,
            features.get("fat_sources"),
            features.get("fat_source_types"),
            features.get("antioxidant_sources"),
            features.get("antioxidant_types"),
            features.get("micronutrient_sources"),
            features.get("micronutrient_types"),
            features.get("omega6_sources"),
            features.get("omega3_sources"),
            features.get("profile_status") or "ready",
            features.get("profile_version") or "science-v1",
            features.get("source_fingerprint") or formula.get("ingredient_fingerprint"),
            features.get("needs_review"),
            features.get("review_reason"),
        ),
    )


def _upsert_fiber_feature(
    cursor,
    *,
    formula: dict[str, Any],
    ingredient_text: str,
    feature_json: dict[str, Any],
    starch_ingredients: list[dict[str, Any]],
    profile_status: str = "ready",
    profile_version: str = "science-v1",
    source_fingerprint: str | None = None,
) -> None:
    cursor.execute(
        f"""
        INSERT INTO {FIBER_TARGET_DB}.{FIBER_TARGET_TABLE}(
          formula_id, raw_ingredient_text, ingredient_feature_json, starch_ingredients_json,
          profile_status, profile_version, source_fingerprint
        ) VALUES(%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          raw_ingredient_text = VALUES(raw_ingredient_text),
          ingredient_feature_json = VALUES(ingredient_feature_json),
          starch_ingredients_json = VALUES(starch_ingredients_json),
          profile_status = VALUES(profile_status),
          profile_version = VALUES(profile_version),
          source_fingerprint = VALUES(source_fingerprint),
          updated_at = CURRENT_TIMESTAMP
        """,
        (
            int(formula["formula_id"]),
            ingredient_text,
            json.dumps(feature_json, ensure_ascii=False),
            json.dumps(starch_ingredients, ensure_ascii=False),
            profile_status,
            profile_version,
            source_fingerprint or formula.get("ingredient_fingerprint"),
        ),
    )


def _upsert_candidate(cursor, *, raw_name: str, context: str) -> None:
    normalized = protein_aggregate._normalize_ingredient_key(raw_name)
    if protein_aggregate._ingredient_candidate_noise_reason(normalized):
        return
    cursor.execute(
        f"""
        INSERT INTO `{INGREDIENT_CANDIDATE_TABLE}`(
          raw_name, normalized_raw_name, context, status
        ) VALUES(%s, %s, %s, 'pending')
        ON DUPLICATE KEY UPDATE
          raw_name = IF(status LIKE 'discarded_%%', raw_name, VALUES(raw_name)),
          context = VALUES(context),
          status = IF(status='out_of_scope', 'pending', status),
          updated_at = NOW()
        """,
        (raw_name, normalized, context[:2000] or None),
    )


def backfill_formula(
    formula_id: int, *, apply: bool, materialize_science_features: bool = True
) -> dict[str, Any]:
    engine = _engine()
    standard_lookup = protein_aggregate._load_standard_ingredient_lookup(
        engine,
        standard_db=get_mysql_config()["database"],
        ingredient_table="catfood_standard_ingredient",
        alias_table="catfood_standard_ingredient_alias",
    )
    with _connect() as conn:
        with conn.cursor() as cursor:
            ensure_tables(cursor)
            _ensure_fat_target(cursor)
            _ensure_fiber_target(cursor)
            cursor.execute(
                f"""
                SELECT r.rule_id, r.match_scope, r.match_operator, r.match_value,
                       r.exclude_value, r.feature_domain, r.feature_key,
                       r.feature_value, r.priority, r.confidence,
                       l.dimension_code, l.dimension_name, l.value_code,
                       l.value_name, l.label_content
                FROM `{INGREDIENT_FEATURE_RULE_TABLE}` r
                JOIN `catfood_feature_label` l ON l.label_id = r.label_id
                WHERE r.active = 1 AND l.active = 1
                  AND r.rule_stage = 'ingredient'
                  AND r.feature_domain = 'protein'
                ORDER BY r.priority DESC, r.rule_id
                """
            )
            protein_rules = cursor.fetchall()
            cursor.execute(
                f"""
                SELECT *
                FROM `{FORMULA_INPUT_TABLE}`
                WHERE formula_id = %s
                """,
                (int(formula_id),),
            )
            formula = cursor.fetchone()
            if not formula:
                raise KeyError(f"formula_id 不存在: {formula_id}")

            ingredient_text = formula.get("ingredient_composition") or ""
            main_ingredient_text = _strip_additives(ingredient_text)
            items = protein_aggregate._standardize_ingredient_items(
                main_ingredient_text,
                standard_lookup,
                protein_rules,
            )
            cursor.execute(
                f"SELECT raw_name,status,review_note FROM `{INGREDIENT_CANDIDATE_TABLE}` "
                "WHERE status LIKE 'discarded_%'"
            )
            discarded = {row["raw_name"]: row for row in cursor.fetchall()}
            for item in items:
                # Discard decisions are exact-text decisions. Normalized keys may collapse
                # "鱼油" and "鱼油(三文鱼油、保存剂)" and must not propagate disposal.
                decision = discarded.get(str(item.get("raw_name") or ""))
                structural_discard = _is_structural_compound(item.get("raw_name"))
                item["is_ignored"] = bool(
                    not item.get("standard_ingredient_id") and (decision or structural_discard)
                )
                item["discard_decision"] = decision
            labels = protein_aggregate._protein_labels_from_standard_items(
                main_ingredient_text,
                standard_lookup,
                protein_rules,
            )
            effective_items = [item for item in items if not item.get("is_ignored")]
            status_payload = _domain_status_for_unmatched(effective_items)
            if materialize_science_features:
                science_profiles = _load_science_profiles(cursor, effective_items)
                fat_features = build_science_features(effective_items, science_profiles)
                fiber_payload = build_science_payload(effective_items, science_profiles)

            cursor.execute(
                f"DELETE FROM `{FORMULA_INGREDIENT_ITEM_TABLE}` WHERE formula_id = %s",
                (int(formula_id),),
            )
            for item in items:
                features = _features_for_item(item)
                if not item.get("standard_ingredient_id") and not item.get("is_ignored"):
                    _upsert_candidate(
                        cursor,
                        raw_name=item["raw_name"],
                        context=main_ingredient_text,
                    )
                cursor.execute(
                    f"""
                    INSERT INTO `{FORMULA_INGREDIENT_ITEM_TABLE}`(
                      formula_id, position, raw_name, standard_ingredient_id,
                      standard_name, ingredient_family, source_type, animal_source,
                      primary_nutrition_role, protein_form, modifiers_json,
                      match_method, confidence, is_protein, is_plant_protein,
                      features_json, match_status, review_status, issue_severity,
                      affected_domains_json, is_ignored
                    ) VALUES(
                      %s, %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s, %s,
                      %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        int(formula_id),
                        item["position"],
                        item["raw_name"],
                        item.get("standard_ingredient_id"),
                        item.get("standard_name"),
                        item.get("ingredient_family"),
                        item.get("source_type"),
                        item.get("animal_source"),
                        item.get("primary_nutrition_role"),
                        item.get("protein_form"),
                        json.dumps([], ensure_ascii=False),
                        item.get("match_method"),
                        item.get("confidence"),
                        1 if item.get("is_protein") else 0,
                        1 if item.get("is_plant_protein") else 0,
                        json.dumps(features, ensure_ascii=False),
                        "ignored" if item.get("is_ignored") else "matched" if item.get("standard_ingredient_id") else "unmatched",
                        "discarded" if item.get("is_ignored") else "auto_approved" if item.get("standard_ingredient_id") else "pending",
                        None if item.get("standard_ingredient_id") or item.get("is_ignored") else _domain_status_for_unmatched([item])["unmatched_ingredients"][0]["severity"],
                        json.dumps(
                            [] if item.get("standard_ingredient_id") or item.get("is_ignored")
                            else [
                                domain for domain in _domain_status_for_unmatched([item])["unmatched_ingredients"][0]["likely_domains"]
                                if domain in {"protein", "fat", "fiber", "starch"}
                            ],
                            ensure_ascii=False,
                        ),
                        1 if item.get("is_ignored") else 0,
                    ),
                )

            matched = sum(1 for item in effective_items if item.get("standard_ingredient_id"))
            unmatched = len(effective_items) - matched
            domain_status = dict(status_payload["domain_status"])
            gates = {}
            dirty_domains = []
            for domain in ("protein", "fat", "fiber", "starch"):
                allowed = domain_status.get(domain) != "blocked"
                status = "pending_rebuild" if allowed else "need_review"
                gates[domain] = {
                    "allowed": allowed,
                    "status": status,
                    "reason": None if allowed else "存在未完成标准化的相关原料",
                }
                domain_status[domain] = status
                if allowed:
                    dirty_domains.append(domain)
            allowed_count = len(dirty_domains)
            overall_status = (
                "ready_for_rebuild" if allowed_count == 4
                else "partially_ready" if allowed_count
                else "need_review"
            )
            if materialize_science_features:
                if gates["fat"]["status"] == "need_review":
                    fat_features["profile_status"] = "needs_review"
                _upsert_fat_feature(
                    cursor,
                    formula=formula,
                    ingredient_text=main_ingredient_text,
                    features=fat_features,
                )
                _upsert_fiber_feature(
                    cursor,
                    formula=formula,
                    ingredient_text=main_ingredient_text,
                    feature_json=fiber_payload["ingredient_feature_json"],
                    starch_ingredients=fiber_payload["starch_ingredients_json"],
                    profile_status=fiber_payload["profile_status"],
                    profile_version=fiber_payload["profile_version"],
                    source_fingerprint=fiber_payload["science_source_fingerprint"],
                )
            ignored_count = len(items) - len(effective_items)
            coverage = matched / len(effective_items) if effective_items else 1.0
            quality = {
                "ingredient_count": len(items),
                "effective_ingredient_count": len(effective_items),
                "standardized_count": matched,
                "ignored_count": ignored_count,
                "unmatched_count": unmatched,
                "blocking_count": len(status_payload["blocking_domains"]),
                "warning_count": len(status_payload["warning_domains"]),
                "coverage": round(coverage, 5),
            }
            input_hash = hashlib.sha256(
                json.dumps(
                    {
                        "fingerprint": formula.get("ingredient_fingerprint"),
                        "items": [
                            [item.get("position"), item.get("standard_ingredient_id"), bool(item.get("is_ignored"))]
                            for item in items
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            cursor.execute(
                f"""
                INSERT INTO `{FORMULA_FEATURE_PROFILE_TABLE}`(
                  formula_id, ingredient_fingerprint, ingredient_count,
                  effective_ingredient_count, standardized_ingredient_count,
                  ignored_ingredient_count, unmatched_ingredient_count,
                  blocking_ingredient_count, warning_ingredient_count,
                  standardization_coverage, overall_status,
                  protein_status, fat_status, fiber_status, starch_status,
                  domain_gate_json, dirty_domains_json, quality_metrics_json,
                  input_hash, profile_version, rule_versions_json
                ) VALUES(
                  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  %s,%s,%s,%s,%s,%s,%s,%s,'gate-v1',%s
                )
                ON DUPLICATE KEY UPDATE
                  ingredient_fingerprint=VALUES(ingredient_fingerprint),
                  ingredient_count=VALUES(ingredient_count),
                  effective_ingredient_count=VALUES(effective_ingredient_count),
                  standardized_ingredient_count=VALUES(standardized_ingredient_count),
                  ignored_ingredient_count=VALUES(ignored_ingredient_count),
                  unmatched_ingredient_count=VALUES(unmatched_ingredient_count),
                  blocking_ingredient_count=VALUES(blocking_ingredient_count),
                  warning_ingredient_count=VALUES(warning_ingredient_count),
                  standardization_coverage=VALUES(standardization_coverage),
                  overall_status=VALUES(overall_status),
                  protein_status=VALUES(protein_status), fat_status=VALUES(fat_status),
                  fiber_status=VALUES(fiber_status), starch_status=VALUES(starch_status),
                  domain_gate_json=VALUES(domain_gate_json),
                  dirty_domains_json=VALUES(dirty_domains_json),
                  quality_metrics_json=VALUES(quality_metrics_json),
                  input_hash=VALUES(input_hash),
                  profile_version=VALUES(profile_version),
                  rule_versions_json=VALUES(rule_versions_json),
                  updated_at = NOW()
                """,
                (
                    int(formula_id),
                    formula.get("ingredient_fingerprint"), len(items), len(effective_items), matched,
                    ignored_count, unmatched, len(status_payload["blocking_domains"]),
                    len(status_payload["warning_domains"]), round(coverage, 5), overall_status,
                    domain_status["protein"], domain_status["fat"],
                    domain_status["fiber"], domain_status["starch"],
                    json.dumps(gates, ensure_ascii=False),
                    json.dumps(dirty_domains, ensure_ascii=False),
                    json.dumps(quality, ensure_ascii=False), input_hash,
                    json.dumps({"gate": "gate-v1"}, ensure_ascii=False),
                ),
            )
        if apply:
            conn.commit()
        else:
            conn.rollback()

    return {
        "ok": True,
        "applied": apply,
        "formula_id": int(formula_id),
        "brand": formula.get("brand"),
        "product_name": formula.get("product_name"),
        "item_count": len(items),
        "matched_count": matched,
        "unmatched_count": unmatched,
        "protein_source_details": labels.get("protein_source_details"),
        "main_protein_form": labels.get("primary_meat_source_type"),
        "secondary_protein_form": labels.get("secondary_meat_source_type"),
        "meat_source_complexity": labels.get("meat_source_complexity"),
        "plant_protein_sources": labels.get("plant_protein_labels"),
        "protein_rule_count": len(protein_rules),
        "science_features_materialized": bool(materialize_science_features),
    }


def materialize_formula_science_source_tables(
    formula_id: int, *, apply: bool = True
) -> dict[str, Any]:
    """Write fat/fiber layer-4 sources from standardized items and active science profiles."""
    with _connect() as conn:
        with conn.cursor() as cursor:
            _ensure_fat_target(cursor)
            _ensure_fiber_target(cursor)
            cursor.execute(
                f"SELECT * FROM `{FORMULA_INPUT_TABLE}` WHERE formula_id=%s", (int(formula_id),)
            )
            formula = cursor.fetchone()
            if not formula:
                raise KeyError(f"formula_id 不存在: {formula_id}")
            cursor.execute(
                f"SELECT * FROM `{FORMULA_INGREDIENT_ITEM_TABLE}` "
                "WHERE formula_id=%s ORDER BY position",
                (int(formula_id),),
            )
            items = [item for item in cursor.fetchall() if not item.get("is_ignored")]
            profiles = _load_science_profiles(cursor, items)
            fat_features = build_science_features(items, profiles)
            fiber_payload = build_science_payload(items, profiles)
            ingredient_text = _strip_additives(formula.get("ingredient_composition") or "")
            _upsert_fat_feature(
                cursor, formula=formula, ingredient_text=ingredient_text, features=fat_features
            )
            _upsert_fiber_feature(
                cursor,
                formula=formula,
                ingredient_text=ingredient_text,
                feature_json=fiber_payload["ingredient_feature_json"],
                starch_ingredients=fiber_payload["starch_ingredients_json"],
                profile_status=fiber_payload["profile_status"],
                profile_version=fiber_payload["profile_version"],
                source_fingerprint=fiber_payload["science_source_fingerprint"],
            )
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return {
        "ok": True,
        "applied": bool(apply),
        "formula_id": int(formula_id),
        "fat_profile_status": fat_features["profile_status"],
        "fiber_profile_status": fiber_payload["profile_status"],
        "fat_missing_science_count": len(fat_features["missing_science_profiles"]),
        "fiber_missing_science_count": len(fiber_payload["missing_science_profiles"]),
    }


def backfill_all_formulas(*, apply: bool, limit: int = 0) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            sql = f"""
                SELECT formula_id
                FROM `{FORMULA_INPUT_TABLE}`
                WHERE build_status <> 'blocked'
                  AND EXISTS (
                    SELECT 1 FROM catfood_standard_formula f
                    WHERE f.formula_id = `{FORMULA_INPUT_TABLE}`.formula_id
                  )
                ORDER BY formula_id
            """
            if limit > 0:
                sql += " LIMIT %s"
                cursor.execute(sql, (int(limit),))
            else:
                cursor.execute(sql)
            formula_ids = [int(row["formula_id"]) for row in cursor.fetchall()]

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, formula_id in enumerate(formula_ids, start=1):
        try:
            results.append(backfill_formula(formula_id, apply=apply))
        except Exception as exc:
            failures.append({"formula_id": formula_id, "error": str(exc)})
        if index % 10 == 0 or index == len(formula_ids):
            print(
                f"profile backfill progress: {index}/{len(formula_ids)}, failures={len(failures)}",
                flush=True,
            )
    return {
        "ok": not failures,
        "applied": apply,
        "formula_count": len(formula_ids),
        "succeeded": len(results),
        "failed": len(failures),
        "item_count": sum(int(item.get("item_count") or 0) for item in results),
        "matched_count": sum(int(item.get("matched_count") or 0) for item in results),
        "unmatched_count": sum(int(item.get("unmatched_count") or 0) for item in results),
        "failure_samples": failures[:20],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill ingredient item/profile tables for one formula.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--formula-id", type=int)
    target.add_argument("--all", action="store_true", help="backfill every non-blocked formula input")
    parser.add_argument("--limit", type=int, default=0, help="limit formulas in --all mode")
    parser.add_argument("--apply", action="store_true", help="write changes; default rolls back")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all:
        result = backfill_all_formulas(apply=bool(args.apply), limit=max(0, int(args.limit)))
    else:
        result = backfill_formula(args.formula_id, apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
