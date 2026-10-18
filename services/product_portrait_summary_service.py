"""Rule-based, versioned five-dimension product portrait summaries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import pymysql

from app_config import get_feature_mysql_config


TABLE = "catfood_formula_portrait_summary"
DEFAULT_FEATURE_VERSION = "soft_v1"
PORTRAIT_VERSION = "six_dimension_v2"


def _connect():
    return pymysql.connect(
        **get_feature_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
    )


def ensure_portrait_table(conn=None) -> None:
    owns_connection = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS `{TABLE}` (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    formula_id BIGINT UNSIGNED NOT NULL,
                    feature_version VARCHAR(32) NOT NULL,
                    portrait_version VARCHAR(32) NOT NULL,
                    protein_quality_score DECIMAL(10,4) NULL,
                    protein_quality_level VARCHAR(32) NULL,
                    protein_quality_label VARCHAR(255) NULL,
                    protein_score DECIMAL(10,4) NULL,
                    protein_level VARCHAR(32) NULL,
                    protein_label VARCHAR(255) NULL,
                    fat_score DECIMAL(10,4) NULL,
                    fat_level VARCHAR(32) NULL,
                    fat_label VARCHAR(255) NULL,
                    fiber_score DECIMAL(10,4) NULL,
                    fiber_level VARCHAR(32) NULL,
                    fiber_label VARCHAR(255) NULL,
                    carb_score DECIMAL(10,4) NULL,
                    carb_level VARCHAR(32) NULL,
                    carb_label VARCHAR(255) NULL,
                    prebiotic_score DECIMAL(10,4) NULL,
                    prebiotic_level VARCHAR(32) NULL,
                    prebiotic_label VARCHAR(255) NULL,
                    headline VARCHAR(255) NULL,
                    summary TEXT NULL,
                    portrait_json JSON NULL,
                    source_feature_id BIGINT UNSIGNED NULL,
                    source_data_hash CHAR(64) NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_formula_portrait_version
                        (formula_id, feature_version, portrait_version),
                    KEY idx_portrait_formula_status (formula_id, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute("SELECT DATABASE() AS database_name")
            database_name = cursor.fetchone()["database_name"]
            cursor.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema=%s AND table_name=%s
                     AND column_name IN ('protein_quality_score','protein_quality_level','protein_quality_label')""",
                (database_name, TABLE),
            )
            existing_columns = {row["column_name"] for row in cursor.fetchall()}
            for column_name, definition in (
                ("protein_quality_score", "DECIMAL(10,4) NULL AFTER portrait_version"),
                ("protein_quality_level", "VARCHAR(32) NULL AFTER protein_quality_score"),
                ("protein_quality_label", "VARCHAR(255) NULL AFTER protein_quality_level"),
            ):
                if column_name not in existing_columns:
                    cursor.execute(
                        f"ALTER TABLE `{TABLE}` ADD COLUMN `{column_name}` {definition}"
                    )
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _burden_level(value: float | None, scale_max: float) -> tuple[str, str]:
    if value is None:
        return "missing", "资料不足"
    ratio = max(0.0, min(1.0, value / scale_max))
    low_max = 0.35 if scale_max == 1.0 else 0.25
    medium_max = 0.65 if scale_max == 1.0 else 0.50
    if ratio <= low_max:
        return "low", "较低"
    if ratio <= medium_max:
        return "medium_low", "中低"
    if ratio <= 0.75:
        return "elevated", "偏明显"
    return "high", "较高"


def _support_level(value: float | None, scale_max: float) -> tuple[str, str]:
    if value is None:
        return "missing", "资料不足"
    ratio = max(0.0, min(1.0, value / scale_max))
    if ratio <= 0.25:
        return "low", "偏低"
    if ratio <= 0.50:
        return "medium_low", "中低"
    if ratio <= 0.75:
        return "medium_high", "中高"
    return "high", "较高"


def _source_hash(row: dict[str, Any]) -> str:
    fields = (
        "formula_id", "id", "feature_version", "protein_quality_score",
        "protein_score", "fat_score",
        "fiber_score", "carb_score", "prebiotic_score", "fat_structure_type",
        "fat_reason_tags", "p_level", "q_level", "pq_combo_name",
    )
    payload = {key: str(row.get(key)) if row.get(key) is not None else None for key in fields}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def build_portrait(row: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic user-facing portrait from one feature row."""
    protein_quality = _number(row.get("protein_quality_score"))
    protein = _number(row.get("protein_score"))
    fat = _number(row.get("fat_score"))
    fiber = _number(row.get("fiber_score"))
    carb = _number(row.get("carb_score"))
    prebiotic = _number(row.get("prebiotic_score"))

    protein_quality_level, protein_quality_text = _support_level(protein_quality, 1.0)
    protein_level, protein_text = _burden_level(protein, 1.0)
    fat_level, fat_text = _burden_level(fat, 1.0)
    fiber_level, fiber_text = _support_level(fiber, 5.0)
    carb_level, carb_text = _burden_level(carb, 5.0)
    prebiotic_level, prebiotic_text = _support_level(prebiotic, 5.0)

    protein_quality_label = f"蛋白质量{protein_quality_text}"
    protein_label = f"蛋白压力{protein_text}"
    fat_label = f"脂肪负担{fat_text}"
    fiber_label = "高纤维托底" if fiber_level == "high" else f"纤维支持{fiber_text}"
    carb_label = f"碳水负担{carb_text}"
    prebiotic_label = f"益生元支持{prebiotic_text}"

    fat_structure = str(row.get("fat_structure_type") or "").strip()
    fat_reasons = [
        item.strip() for item in str(row.get("fat_reason_tags") or "").split(",") if item.strip()
    ]
    is_complex_fat = "复合" in fat_structure or any("复杂" in item for item in fat_reasons)
    fat_clause = fat_label
    if is_complex_fat:
        fat_clause = f"脂肪来源复杂且{fat_label}"

    headline = f"{protein_quality_label}，{fiber_label}，{fat_label}"
    connector = "但" if fat_level in {"elevated", "high"} else "同时"
    summary = (
        f"{protein_quality_label}，{fiber_label}、{prebiotic_label}，{protein_label}，"
        f"{connector}{fat_clause}；{carb_label}。"
    )

    dimensions = {
        "protein_quality": {"score": protein_quality, "scale_max": 1,
                            "level": protein_quality_level,
                            "label": protein_quality_label,
                            "direction": "higher_is_support"},
        "protein": {"score": protein, "scale_max": 1, "level": protein_level,
                    "label": protein_label, "direction": "higher_is_burden"},
        "fat": {"score": fat, "scale_max": 1, "level": fat_level,
                "label": fat_label, "direction": "higher_is_burden"},
        "fiber": {"score": fiber, "scale_max": 5, "level": fiber_level,
                  "label": fiber_label, "direction": "higher_is_support"},
        "carbohydrate": {"score": carb, "scale_max": 5, "level": carb_level,
                         "label": carb_label, "direction": "higher_is_burden"},
        "prebiotic": {"score": prebiotic, "scale_max": 5, "level": prebiotic_level,
                      "label": prebiotic_label, "direction": "higher_is_support"},
    }
    return {
        "formula_id": int(row["formula_id"]),
        "product_key": row.get("sku_id") or row.get("product_key"),
        "brand": row.get("brand_name") or row.get("brand"),
        "product_name": row.get("sku_name") or row.get("product_name"),
        "feature_version": str(row.get("feature_version") or DEFAULT_FEATURE_VERSION),
        "portrait_version": PORTRAIT_VERSION,
        "headline": headline,
        "summary": summary,
        "dimensions": dimensions,
        "evidence": {
            "fat_structure_type": fat_structure or None,
            "fat_reason_tags": fat_reasons,
            "p_level": row.get("p_level"),
            "q_level": row.get("q_level"),
            "pq_combo_name": row.get("pq_combo_name"),
        },
        "source_feature_id": int(row["id"]) if row.get("id") is not None else None,
        "source_data_hash": _source_hash(row),
    }


def _load_feature_rows(
    cursor, feature_version: str, formula_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    formula_filter = ""
    params: list[Any] = [feature_version]
    if formula_ids:
        marks = ",".join(["%s"] * len(formula_ids))
        formula_filter = f" AND formula_id IN ({marks})"
        params.extend(formula_ids)
    cursor.execute(
        f"""
        SELECT s.id, s.formula_id, s.sku_id, s.sku_name, s.brand_name,
               s.feature_version, w.protein_quality_score,
               s.protein_score, s.fat_score, s.fiber_score,
               s.carb_score, s.prebiotic_score,
               w.product_key, w.brand, w.product_name, w.fat_structure_type,
               w.fat_reason_tags, w.p_level, w.q_level,
               fs.pq_combo_name
        FROM sku_feature_input s
        JOIN (
            SELECT formula_id, MAX(id) AS latest_id
            FROM sku_feature_input
            WHERE feature_version=%s AND formula_id IS NOT NULL
              {formula_filter}
            GROUP BY formula_id
        ) latest ON latest.latest_id=s.id
        JOIN csv_labeling.catfood_standard_formula f
          ON f.formula_id=s.formula_id AND f.status='active' AND f.is_current=1
        LEFT JOIN catfood_protein_fat_fiber_score_wide w
          ON w.formula_id=s.formula_id
        LEFT JOIN catfood_fiber_feature_score fs
          ON fs.id=(
              SELECT MAX(fs_latest.id)
              FROM catfood_fiber_feature_score fs_latest
              WHERE fs_latest.formula_id=s.formula_id
          )
        ORDER BY s.formula_id
        """,
        params,
    )
    return list(cursor.fetchall())


def rebuild_all_product_portraits(
    *, feature_version: str = DEFAULT_FEATURE_VERSION, force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    feature_version = str(feature_version or "").strip()
    if not feature_version or len(feature_version) > 32:
        raise ValueError("feature_version 无效")
    now = datetime.now()
    result = {"ok": True, "feature_version": feature_version,
              "portrait_version": PORTRAIT_VERSION, "dry_run": dry_run,
              "total": 0, "created": 0, "updated": 0, "unchanged": 0,
              "items": []}

    with _connect() as conn:
        ensure_portrait_table(conn)
        with conn.cursor() as cursor:
            rows = _load_feature_rows(cursor, feature_version)
            result["total"] = len(rows)
            cursor.execute(
                f"SELECT formula_id,source_data_hash FROM `{TABLE}` "
                "WHERE feature_version=%s AND portrait_version=%s",
                (feature_version, PORTRAIT_VERSION),
            )
            existing = {int(row["formula_id"]): row["source_data_hash"] for row in cursor.fetchall()}
            for row in rows:
                portrait = build_portrait(row)
                formula_id = portrait["formula_id"]
                old_hash = existing.get(formula_id)
                action = "unchanged" if old_hash == portrait["source_data_hash"] and not force else (
                    "updated" if old_hash is not None else "created"
                )
                result[action] += 1
                result["items"].append({"formula_id": formula_id, "product_name": portrait["product_name"],
                                        "action": action, "summary": portrait["summary"]})
                if dry_run or action == "unchanged":
                    continue
                d = portrait["dimensions"]
                cursor.execute(
                    f"""
                    INSERT INTO `{TABLE}` (
                        formula_id,feature_version,portrait_version,
                        protein_quality_score,protein_quality_level,protein_quality_label,
                        protein_score,protein_level,protein_label,
                        fat_score,fat_level,fat_label,
                        fiber_score,fiber_level,fiber_label,
                        carb_score,carb_level,carb_label,
                        prebiotic_score,prebiotic_level,prebiotic_label,
                        headline,summary,portrait_json,source_feature_id,
                        source_data_hash,status,created_at,updated_at
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s
                    ) ON DUPLICATE KEY UPDATE
                        protein_quality_score=VALUES(protein_quality_score),protein_quality_level=VALUES(protein_quality_level),protein_quality_label=VALUES(protein_quality_label),
                        protein_score=VALUES(protein_score),protein_level=VALUES(protein_level),protein_label=VALUES(protein_label),
                        fat_score=VALUES(fat_score),fat_level=VALUES(fat_level),fat_label=VALUES(fat_label),
                        fiber_score=VALUES(fiber_score),fiber_level=VALUES(fiber_level),fiber_label=VALUES(fiber_label),
                        carb_score=VALUES(carb_score),carb_level=VALUES(carb_level),carb_label=VALUES(carb_label),
                        prebiotic_score=VALUES(prebiotic_score),prebiotic_level=VALUES(prebiotic_level),prebiotic_label=VALUES(prebiotic_label),
                        headline=VALUES(headline),summary=VALUES(summary),portrait_json=VALUES(portrait_json),
                        source_feature_id=VALUES(source_feature_id),source_data_hash=VALUES(source_data_hash),
                        status='active',updated_at=VALUES(updated_at)
                    """,
                    (
                        formula_id, feature_version, PORTRAIT_VERSION,
                        d["protein_quality"]["score"], d["protein_quality"]["level"], d["protein_quality"]["label"],
                        d["protein"]["score"], d["protein"]["level"], d["protein"]["label"],
                        d["fat"]["score"], d["fat"]["level"], d["fat"]["label"],
                        d["fiber"]["score"], d["fiber"]["level"], d["fiber"]["label"],
                        d["carbohydrate"]["score"], d["carbohydrate"]["level"], d["carbohydrate"]["label"],
                        d["prebiotic"]["score"], d["prebiotic"]["level"], d["prebiotic"]["label"],
                        portrait["headline"], portrait["summary"],
                        json.dumps(portrait, ensure_ascii=False), portrait["source_feature_id"],
                        portrait["source_data_hash"], now, now,
                    ),
                )
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    return result


def rebuild_product_portraits(
    formula_ids: list[int], *, feature_version: str = DEFAULT_FEATURE_VERSION,
    force: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    """Incrementally rebuild deterministic portraits for specific formulas."""
    ids = sorted({int(value) for value in formula_ids if value is not None})
    if not ids:
        raise ValueError("formula_ids 不能为空")
    feature_version = str(feature_version or "").strip()
    if not feature_version or len(feature_version) > 32:
        raise ValueError("feature_version 无效")

    now = datetime.now()
    result = {"ok": True, "feature_version": feature_version,
              "portrait_version": PORTRAIT_VERSION, "dry_run": dry_run,
              "requested_formula_ids": ids, "total": 0, "created": 0,
              "updated": 0, "unchanged": 0, "items": []}
    with _connect() as conn:
        ensure_portrait_table(conn)
        with conn.cursor() as cursor:
            rows = _load_feature_rows(cursor, feature_version, ids)
            found = {int(row["formula_id"]) for row in rows}
            missing = [formula_id for formula_id in ids if formula_id not in found]
            if missing:
                raise ValueError(
                    "缺少当前启用配方的最新特征数据: " + ",".join(map(str, missing))
                )
            result["total"] = len(rows)
            marks = ",".join(["%s"] * len(ids))
            cursor.execute(
                f"SELECT formula_id,source_data_hash FROM `{TABLE}` "
                f"WHERE feature_version=%s AND portrait_version=%s AND formula_id IN ({marks})",
                (feature_version, PORTRAIT_VERSION, *ids),
            )
            existing = {int(row["formula_id"]): row["source_data_hash"] for row in cursor.fetchall()}
            for row in rows:
                portrait = build_portrait(row)
                formula_id = portrait["formula_id"]
                old_hash = existing.get(formula_id)
                action = "unchanged" if old_hash == portrait["source_data_hash"] and not force else (
                    "updated" if old_hash is not None else "created"
                )
                result[action] += 1
                result["items"].append({"formula_id": formula_id,
                                        "source_feature_id": portrait["source_feature_id"],
                                        "action": action, "summary": portrait["summary"]})
                if dry_run or action == "unchanged":
                    continue
                d = portrait["dimensions"]
                cursor.execute(
                    f"""INSERT INTO `{TABLE}` (
                        formula_id,feature_version,portrait_version,
                        protein_quality_score,protein_quality_level,protein_quality_label,
                        protein_score,protein_level,protein_label,fat_score,fat_level,fat_label,
                        fiber_score,fiber_level,fiber_label,carb_score,carb_level,carb_label,
                        prebiotic_score,prebiotic_level,prebiotic_label,headline,summary,
                        portrait_json,source_feature_id,source_data_hash,status,created_at,updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                    ON DUPLICATE KEY UPDATE
                        protein_quality_score=VALUES(protein_quality_score),protein_quality_level=VALUES(protein_quality_level),protein_quality_label=VALUES(protein_quality_label),
                        protein_score=VALUES(protein_score),protein_level=VALUES(protein_level),protein_label=VALUES(protein_label),
                        fat_score=VALUES(fat_score),fat_level=VALUES(fat_level),fat_label=VALUES(fat_label),
                        fiber_score=VALUES(fiber_score),fiber_level=VALUES(fiber_level),fiber_label=VALUES(fiber_label),
                        carb_score=VALUES(carb_score),carb_level=VALUES(carb_level),carb_label=VALUES(carb_label),
                        prebiotic_score=VALUES(prebiotic_score),prebiotic_level=VALUES(prebiotic_level),prebiotic_label=VALUES(prebiotic_label),
                        headline=VALUES(headline),summary=VALUES(summary),portrait_json=VALUES(portrait_json),
                        source_feature_id=VALUES(source_feature_id),source_data_hash=VALUES(source_data_hash),
                        status='active',updated_at=VALUES(updated_at)""",
                    (formula_id, feature_version, PORTRAIT_VERSION,
                     d["protein_quality"]["score"], d["protein_quality"]["level"], d["protein_quality"]["label"],
                     d["protein"]["score"], d["protein"]["level"], d["protein"]["label"],
                     d["fat"]["score"], d["fat"]["level"], d["fat"]["label"],
                     d["fiber"]["score"], d["fiber"]["level"], d["fiber"]["label"],
                     d["carbohydrate"]["score"], d["carbohydrate"]["level"], d["carbohydrate"]["label"],
                     d["prebiotic"]["score"], d["prebiotic"]["level"], d["prebiotic"]["label"],
                     portrait["headline"], portrait["summary"], json.dumps(portrait, ensure_ascii=False),
                     portrait["source_feature_id"], portrait["source_data_hash"], now, now),
                )
        conn.rollback() if dry_run else conn.commit()
    return result


def materialize_product_portrait(payload: dict[str, Any]) -> dict[str, Any]:
    """Pipeline adapter that writes and validates one chat-consumable portrait."""
    formula_id = int(payload.get("formula_id") or 0)
    if formula_id <= 0:
        raise ValueError("formula_id 不能为空")
    result = rebuild_product_portraits([formula_id])
    from services.guardian_product_portrait_service import load_product_portraits
    portrait = load_product_portraits([formula_id]).get(formula_id)
    if not portrait:
        raise RuntimeError("画像写入后校验失败")
    return {"ok": True, "formula_id": formula_id,
            "feature_version": DEFAULT_FEATURE_VERSION,
            "portrait_version": PORTRAIT_VERSION,
            "source_feature_id": portrait.get("source_feature_id"),
            "summary": portrait.get("summary"), "build": result}
