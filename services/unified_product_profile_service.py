"""Materialize one stable, comparison-ready profile for every formula."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Any

import pymysql

from app_config import get_feature_mysql_config
from services.product_portrait_summary_service import (
    DEFAULT_FEATURE_VERSION,
    PORTRAIT_VERSION,
    _load_feature_rows,
    build_portrait,
    validated_feature_row,
)


TABLE = "catfood_unified_product_profile"
PROFILE_VERSION = "unified_product_profile_v1"
MARKET_LABELS = {
    "protein_quality": "动物蛋白",
    "fat": "脂肪",
    "fiber": "纤维",
    "carbohydrate": "淀粉碳水",
    "prebiotic": "益生元",
}
DIMENSION_LABELS = {
    "protein_quality": "蛋白质量",
    "protein": "蛋白压力",
    "fat": "脂肪负担",
    "fiber": "纤维支持",
    "carbohydrate": "碳水负担",
    "prebiotic": "益生元支持",
}


def _connect():
    return pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor)


def _json(value: Any, fallback: Any):
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def ensure_table(conn=None) -> None:
    owns = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""CREATE TABLE IF NOT EXISTS `{TABLE}` (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                formula_id BIGINT UNSIGNED NOT NULL,
                product_key VARCHAR(255) NULL,
                brand VARCHAR(100) NULL,
                product_name VARCHAR(255) NULL,
                profile_version VARCHAR(64) NOT NULL,
                feature_version VARCHAR(32) NOT NULL,
                portrait_version VARCHAR(32) NOT NULL,
                market_version VARCHAR(64) NOT NULL,
                structure_profile_json JSON NOT NULL,
                six_dimension_json JSON NOT NULL,
                market_performance_json JSON NOT NULL,
                brand_performance_json JSON NOT NULL,
                strengths_json JSON NOT NULL,
                cautions_json JSON NOT NULL,
                data_notes_json JSON NOT NULL,
                profile_json JSON NOT NULL,
                source_data_hash CHAR(64) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                UNIQUE KEY uk_unified_formula_version (formula_id, profile_version),
                KEY idx_unified_brand (brand),
                KEY idx_unified_status (status, updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        conn.commit()
    finally:
        if owns:
            conn.close()


def classify_structure(structures: list[dict[str, Any]]) -> str:
    protein = [str(x.get("level2") or "") for x in structures if x.get("level1") == "蛋白结构"]
    carb = [str(x.get("level2") or "") for x in structures if x.get("level1") == "碳水/淀粉结构"]
    if "多肉源结构" in protein:
        if "豆类淀粉结构" in carb:
            return "多肉源+豆类淀粉型"
        if any("谷物" in item for item in carb):
            return "多肉源+谷物淀粉型"
        return "多肉源复合蛋白型"
    if any("鲜肉" in item or "肉粉" in item for item in protein):
        return "鲜肉/肉粉复合蛋白型"
    return "其他蛋白结构型"


def build_structure_profile(structures: list[dict[str, Any]]) -> dict[str, Any]:
    priority = ("蛋白结构", "碳水/淀粉结构", "肠道功能结构", "纤维结构", "脂肪结构")
    core = []
    for group in priority:
        for item in structures:
            if item.get("level1") == group and item.get("level2") and item.get("level2") not in [x["label"] for x in core]:
                core.append({"dimension": group, "label": item["level2"]})
                if len(core) == 4:
                    break
        if len(core) == 4:
            break
    labels = [item["label"] for item in core]
    return {
        "primary_type": classify_structure(structures),
        "headline": "，".join(labels) if labels else "结构资料不足",
        "core_features": core,
        "all_features": structures,
    }


def build_assessment(portrait: dict[str, Any], market: dict[str, Any]) -> tuple[list[dict], list[dict], list[dict]]:
    strengths, cautions, notes = [], [], []
    rankings = market.get("rankings") or {}
    market_advantages = set(market.get("advantages") or [])
    market_weaknesses = set(market.get("weaknesses") or [])
    for name, item in (portrait.get("dimensions") or {}).items():
        level = item.get("level")
        direction = item.get("direction")
        label = DIMENSION_LABELS.get(name, name)
        market_label = MARKET_LABELS.get(name)
        percentile = rankings.get(market_label) if market_label else None
        basis = {"absolute_level": level, "score": item.get("score"), "scale_max": item.get("scale_max"),
                 "market_label": market_label, "market_percentile": percentile}
        favourable = direction == "higher_is_support" and level == "high" or direction == "higher_is_burden" and level == "low"
        unfavourable = direction == "higher_is_support" and level == "low" or direction == "higher_is_burden" and level in {"elevated", "high"}
        if favourable:
            suffix = ("，且在当前市场中相对突出" if market_label in market_advantages else
                      "，但在当前市场中差异化不强" if market_label in market_weaknesses else "")
            strengths.append({"dimension": name, "title": label, "summary": item.get("label", label) + suffix, "basis": basis})
        elif unfavourable:
            suffix = "，在当前市场中也处于相对弱项" if market_label in market_weaknesses else ""
            cautions.append({"dimension": name, "title": label, "summary": item.get("label", label) + suffix, "basis": basis})
        elif market_label in market_advantages:
            strengths.append({"dimension": name, "title": label,
                              "summary": f"{label}在当前市场中相对突出，但绝对水平为{item.get('label') or level}", "basis": basis})
        if level == "missing":
            notes.append({"dimension": name, "text": f"{label}资料不足"})
    covered = {x["basis"].get("market_label") for x in strengths + cautions}
    for label in market.get("advantages") or []:
        if label not in covered:
            strengths.append({"dimension": None, "title": label, "summary": f"{label}市场表现相对突出",
                              "basis": {"market_label": label, "market_percentile": rankings.get(label)}})
    for label in market.get("weaknesses") or []:
        if label not in covered:
            cautions.append({"dimension": None, "title": label,
                             "summary": f"{label}在当前市场中不突出，不代表不达标或一定不适合",
                             "basis": {"market_label": label, "market_percentile": rankings.get(label)}})
    return strengths, cautions, notes


def _percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    return values[min(int(len(values) * fraction), len(values) - 1)] if values else 100 * fraction


def build_brand_performance(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    global_values: dict[str, list[float]] = defaultdict(list)
    brands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rankings = row["market_performance"]["rankings"]
        brands[row.get("brand") or ""].append(row)
        for key, value in rankings.items():
            if isinstance(value, (int, float)):
                global_values[key].append(float(value))
    p75 = {k: _percentile(v, .75) for k, v in global_values.items()}
    p25 = {k: _percentile(v, .25) for k, v in global_values.items()}
    result = {}
    for brand, items in brands.items():
        if not brand:
            continue
        vals: dict[str, list[float]] = defaultdict(list)
        types: dict[str, int] = defaultdict(int)
        for row in items:
            types[row["structure_profile"]["primary_type"]] += 1
            for key, value in row["market_performance"]["rankings"].items():
                if isinstance(value, (int, float)):
                    vals[key].append(float(value))
        averages = {k: round(sum(v) / len(v), 1) for k, v in vals.items() if v}
        result[brand] = {
            "product_count": len(items),
            "main_portrait": max(types, key=types.get),
            "average_rankings": averages,
            "composite_percentile": round(sum(averages.values()) / len(averages), 1) if averages else None,
            "advantages": [k for k, v in averages.items() if v >= p75.get(k, 75)],
            "weaknesses": [k for k, v in averages.items() if v <= p25.get(k, 25)],
        }
    return result


def build_unified_profile(row: dict[str, Any], portrait: dict[str, Any], brand_performance: dict[str, Any], market_version: str) -> dict[str, Any]:
    structures = _json(row.get("structure_labels"), [])
    market = {"rankings": _json(row.get("market_rankings"), {}),
              "advantages": _json(row.get("advantage_tags"), []),
              "weaknesses": _json(row.get("weakness_tags"), []),
              "market_version": market_version}
    strengths, cautions, notes = build_assessment(portrait, market)
    profile = {
        "schema": "unified_product_profile", "version": PROFILE_VERSION,
        "formula_id": int(row["formula_id"]),
        "product": {k: row.get(k) for k in ("product_key", "brand", "product_name")},
        "structure_profile": build_structure_profile(structures),
        "six_dimensions": portrait,
        "market_performance": market,
        "brand_performance": brand_performance,
        "standalone_assessment": {"strengths": strengths, "cautions": cautions, "data_notes": notes},
        "versions": {"feature": DEFAULT_FEATURE_VERSION, "portrait": PORTRAIT_VERSION,
                     "market": market_version, "profile": PROFILE_VERSION},
    }
    profile["source_data_hash"] = _hash(profile)
    return profile


def rebuild_unified_product_profiles(*, dry_run: bool = False) -> dict[str, Any]:
    now = datetime.now()
    with _connect() as conn:
        ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT formula_id,product_key,brand,product_name,structure_labels,market_rankings,
                          advantage_tags,weakness_tags FROM catfood_formula_profile
                          WHERE structure_labels IS NOT NULL""")
            market_rows = list(cur.fetchall())
            features = _load_feature_rows(cur, DEFAULT_FEATURE_VERSION, [int(r["formula_id"]) for r in market_rows], include_drafts=False)
        market_version = now.strftime("market-%Y%m%d-") + _hash([
            {key: row.get(key) for key in ("formula_id", "structure_labels", "market_rankings", "advantage_tags", "weakness_tags")}
            for row in market_rows
        ])[:8]
        feature_map = {int(r["formula_id"]): build_portrait(validated_feature_row(r)) for r in features}
        prepared = []
        for row in market_rows:
            portrait = feature_map.get(int(row["formula_id"]))
            if not portrait:
                continue
            prepared.append({**row, "structure_profile": build_structure_profile(_json(row.get("structure_labels"), [])),
                             "market_performance": {"rankings": _json(row.get("market_rankings"), {}),
                                                    "advantages": _json(row.get("advantage_tags"), []),
                                                    "weaknesses": _json(row.get("weakness_tags"), [])}})
        brands = build_brand_performance(prepared)
        profiles = [build_unified_profile(row, feature_map[int(row["formula_id"])], brands.get(row.get("brand") or "", {}), market_version)
                    for row in market_rows if int(row["formula_id"]) in feature_map]
        if not dry_run:
            with conn.cursor() as cur:
                for profile in profiles:
                    product = profile["product"]
                    assessment = profile["standalone_assessment"]
                    payloads = [profile["structure_profile"], profile["six_dimensions"], profile["market_performance"],
                                profile["brand_performance"], assessment["strengths"], assessment["cautions"],
                                assessment["data_notes"], profile]
                    cur.execute(f"""INSERT INTO `{TABLE}`
                        (formula_id,product_key,brand,product_name,profile_version,feature_version,portrait_version,
                         market_version,structure_profile_json,six_dimension_json,market_performance_json,
                         brand_performance_json,strengths_json,cautions_json,data_notes_json,profile_json,
                         source_data_hash,status,created_at,updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                        ON DUPLICATE KEY UPDATE product_key=VALUES(product_key),brand=VALUES(brand),product_name=VALUES(product_name),
                         feature_version=VALUES(feature_version),portrait_version=VALUES(portrait_version),market_version=VALUES(market_version),
                         structure_profile_json=VALUES(structure_profile_json),six_dimension_json=VALUES(six_dimension_json),
                         market_performance_json=VALUES(market_performance_json),brand_performance_json=VALUES(brand_performance_json),
                         strengths_json=VALUES(strengths_json),cautions_json=VALUES(cautions_json),data_notes_json=VALUES(data_notes_json),
                         profile_json=VALUES(profile_json),source_data_hash=VALUES(source_data_hash),status='active',updated_at=VALUES(updated_at)""",
                        (profile["formula_id"], product.get("product_key"), product.get("brand"), product.get("product_name"),
                         PROFILE_VERSION, DEFAULT_FEATURE_VERSION, PORTRAIT_VERSION, market_version,
                         *(json.dumps(x, ensure_ascii=False) for x in payloads), profile["source_data_hash"], now, now))
            conn.commit()
        return {"ok": True, "table": TABLE, "profile_version": PROFILE_VERSION, "market_version": market_version,
                "source_rows": len(market_rows), "built": len(profiles), "skipped": len(market_rows) - len(profiles),
                "dry_run": dry_run}
