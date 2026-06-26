"""Unified catalog for C-side cat-food product selection."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pymysql

from app_config import get_feature_mysql_config
from services.product_function_service import infer_function_positioning, normalize_score, reverse_score, weighted_avg_valid


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TAOBAO_SKU_DIR = Path("/Users/yoghourt/anaconda3/envs/comment_labeler_env/taobao/out_catfood_sku")
DEFAULT_BRAND_EXCEL_PATH = Path("/Users/yoghourt/Downloads/猫粮品牌最终标准化主表_区分进口国产.xlsx")
DEFAULT_BRAND_STANDARD_JSON_PATH = BASE_DIR / "vendor" / "csv_mysql_labeling" / "config" / "catfood_brand_standard.json"

CATALOG_TABLE = "catfood_product_catalog"
BRAND_STANDARD_TABLE = "catfood_brand_standard"
BRAND_ALIAS_TABLE = "catfood_brand_alias"
SCORE_WIDE_TABLE = "catfood_protein_fat_fiber_score_wide"
RISK_RESULT_TABLE = "sku_risk_score_result"
SKU_FEATURE_TABLE = "sku_feature_input"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False)


def _json_loads(value: Any, default: Any = None) -> Any:
    if default is None:
        default = []
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _connect_feature(autocommit: bool = False):
    cfg = get_feature_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _product_key(brand: Any, product_name: Any) -> str:
    brand_text = _clean_text(brand)
    product_text = _clean_text(product_name)
    if brand_text and product_text:
        return f"{brand_text}||{product_text}"
    return brand_text or product_text


def _compact(value: Any) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[【】\[\]()（）:：,，。/\\+|｜_\-]+", "", text)
    return text


def _normalize_product_name(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(r"^[A-Za-z]+", "", text).strip()
    text = re.sub(r"(官方旗舰店|旗舰店|官方|正品|包邮|促销|优惠|券|特惠|限时|秒杀)", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -_｜|")
    return text or _clean_text(value)


def _parse_price(value: Any) -> float | None:
    text = _clean_text(value)
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return round(float(match.group(0)), 2)
    except ValueError:
        return None


def _price_bucket(price: float | None) -> str:
    if price is None:
        return "未知"
    if price < 50:
        return "<50"
    if price < 80:
        return "50-80"
    return "80以上"


def _clean_taobao_url(value: Any) -> str:
    return _clean_text(value)


def _is_valid_taobao_item(item: dict[str, Any]) -> bool:
    item_id = _clean_text(item.get("item_id"))
    url = _clean_taobao_url(item.get("url"))
    title = _clean_text(item.get("title"))
    if not item_id:
        return False
    if "item.htm" not in url:
        return False
    if title.startswith("首页-") or "旗舰店-天猫" in title and not _clean_text(item.get("price")):
        return False
    return True


def _origin_filter_value(value: str) -> str:
    if value in {"国产", "domestic", "国产品牌"}:
        return "国产品牌"
    if value in {"进口", "import", "imported", "进口/国际品牌"}:
        return "进口/国际品牌"
    return value


def ensure_catalog_tables() -> None:
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {BRAND_STANDARD_TABLE} (
                    brand_id BIGINT NULL,
                    standard_brand VARCHAR(255) NOT NULL,
                    origin_type VARCHAR(64) NULL,
                    brand_tier VARCHAR(64) NULL,
                    aliases_json JSON NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    remark VARCHAR(1024) NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (standard_brand),
                    KEY idx_origin_type (origin_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {BRAND_ALIAS_TABLE} (
                    alias VARCHAR(255) NOT NULL,
                    standard_brand VARCHAR(255) NOT NULL,
                    origin_type VARCHAR(64) NULL,
                    remark VARCHAR(1024) NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (alias),
                    KEY idx_standard_brand (standard_brand)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CATALOG_TABLE} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    catalog_key VARCHAR(128) NOT NULL,
                    product_key VARCHAR(1024) NULL,
                    standard_brand VARCHAR(255) NOT NULL,
                    raw_brand VARCHAR(255) NULL,
                    product_name VARCHAR(512) NOT NULL,
                    raw_title VARCHAR(1024) NULL,
                    origin_type VARCHAR(64) NULL,
                    brand_tier VARCHAR(64) NULL,
                    source VARCHAR(64) NOT NULL,
                    source_item_id VARCHAR(128) NULL,
                    source_url TEXT NULL,
                    price DECIMAL(10,2) NULL,
                    price_bucket VARCHAR(32) NULL,
                    food_taste VARCHAR(255) NULL,
                    net_content VARCHAR(255) NULL,
                    sold_text VARCHAR(128) NULL,
                    main_image_url TEXT NULL,
                    main_images_json JSON NULL,
                    sku_variants_json JSON NULL,
                    compare_available TINYINT NOT NULL DEFAULT 0,
                    score_source_id BIGINT NULL,
                    function_scores_json JSON NULL,
                    function_tags_json JSON NULL,
                    warning_tags_json JSON NULL,
                    function_display_text VARCHAR(512) NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    quality_flags_json JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_catalog_key (catalog_key),
                    KEY idx_source_item (source, source_item_id),
                    KEY idx_brand (standard_brand),
                    KEY idx_origin_type (origin_type),
                    KEY idx_compare_available (compare_available),
                    KEY idx_status (status),
                    KEY idx_score_source_id (score_source_id),
                    KEY idx_product_key (product_key(255))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()


def load_brand_maps(excel_path: Path | str = DEFAULT_BRAND_EXCEL_PATH) -> dict[str, Any]:
    path = Path(excel_path).expanduser()
    standard: dict[str, dict[str, Any]] = {}
    alias_to_brand: dict[str, str] = {}
    if not path.exists():
        if DEFAULT_BRAND_STANDARD_JSON_PATH.exists():
            data = _json_loads(DEFAULT_BRAND_STANDARD_JSON_PATH.read_text(encoding="utf-8"), {})
            return {
                "standard": data.get("standard") or {},
                "alias_to_brand": data.get("alias_to_brand") or {},
            }
        return {"standard": standard, "alias_to_brand": alias_to_brand}

    main_df = pd.read_excel(path, sheet_name="最终品牌主表", header=1).fillna("")
    alias_df = pd.read_excel(path, sheet_name="别名映射表").fillna("")

    for _, row in main_df.iterrows():
        brand = _clean_text(row.get("标准品牌名"))
        if not brand:
            continue
        aliases = [
            item.strip()
            for item in re.split(r"[/／,，;；]+", _clean_text(row.get("别名/变体汇总")))
            if item.strip()
        ]
        standard[brand] = {
            "brand_id": row.get("brand_id") if _clean_text(row.get("brand_id")) else None,
            "standard_brand": brand,
            "origin_type": _clean_text(row.get("进口/国产分类")) or "待确认",
            "brand_tier": _clean_text(row.get("品牌分层")),
            "aliases": aliases,
            "status": _clean_text(row.get("状态")) or "active",
            "remark": _clean_text(row.get("备注")),
        }
        alias_to_brand[_compact(brand)] = brand
        for alias in aliases:
            alias_to_brand[_compact(alias)] = brand

    for _, row in alias_df.iterrows():
        alias = _clean_text(row.get("别名/变体"))
        brand = _clean_text(row.get("标准品牌名"))
        if alias and brand:
            alias_to_brand[_compact(alias)] = brand
            standard.setdefault(
                brand,
                {
                    "brand_id": None,
                    "standard_brand": brand,
                    "origin_type": _clean_text(row.get("进口/国产分类")) or "待确认",
                    "brand_tier": "",
                    "aliases": [],
                    "status": "active",
                    "remark": _clean_text(row.get("备注")),
                },
            )
    return {"standard": standard, "alias_to_brand": alias_to_brand}


def standardize_brand(raw_brand: Any, title: Any, brand_maps: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    flags: list[str] = []
    standard = brand_maps.get("standard") or {}
    alias_to_brand = brand_maps.get("alias_to_brand") or {}
    candidates = [_clean_text(raw_brand)]
    title_text = _clean_text(title)
    if title_text:
        candidates.extend(re.findall(r"[A-Za-z][A-Za-z0-9!.\- ]{1,24}|[\u4e00-\u9fff]{2,8}", title_text))

    for candidate in candidates:
        compact = _compact(candidate)
        if not compact:
            continue
        if compact in alias_to_brand:
            brand = alias_to_brand[compact]
            return brand, standard.get(brand, {}), flags
        for alias_compact, brand in alias_to_brand.items():
            if alias_compact and alias_compact in compact:
                return brand, standard.get(brand, {}), flags

    fallback = _clean_text(raw_brand)
    if not fallback:
        fallback = _clean_text((title_text.split() or [""])[0])
    flags.append("brand_unmatched")
    return fallback or "未知品牌", {}, flags


def upsert_brand_tables(brand_maps: dict[str, Any]) -> dict[str, int]:
    ensure_catalog_tables()
    standards = list((brand_maps.get("standard") or {}).values())
    alias_rows = []
    for brand in standards:
        for alias in brand.get("aliases") or []:
            alias_rows.append({
                "alias": alias,
                "standard_brand": brand["standard_brand"],
                "origin_type": brand.get("origin_type"),
                "remark": brand.get("remark"),
            })
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            if standards:
                cursor.executemany(
                    f"""
                    INSERT INTO {BRAND_STANDARD_TABLE} (
                        brand_id, standard_brand, origin_type, brand_tier,
                        aliases_json, status, remark
                    ) VALUES (
                        %(brand_id)s, %(standard_brand)s, %(origin_type)s, %(brand_tier)s,
                        %(aliases_json)s, %(status)s, %(remark)s
                    )
                    ON DUPLICATE KEY UPDATE
                        brand_id = VALUES(brand_id),
                        origin_type = VALUES(origin_type),
                        brand_tier = VALUES(brand_tier),
                        aliases_json = VALUES(aliases_json),
                        status = VALUES(status),
                        remark = VALUES(remark)
                    """,
                    [
                        {
                            **row,
                            "aliases_json": _json_dumps(row.get("aliases") or []),
                        }
                        for row in standards
                    ],
                )
            if alias_rows:
                cursor.executemany(
                    f"""
                    INSERT INTO {BRAND_ALIAS_TABLE} (
                        alias, standard_brand, origin_type, remark
                    ) VALUES (
                        %(alias)s, %(standard_brand)s, %(origin_type)s, %(remark)s
                    )
                    ON DUPLICATE KEY UPDATE
                        standard_brand = VALUES(standard_brand),
                        origin_type = VALUES(origin_type),
                        remark = VALUES(remark)
                    """,
                    alias_rows,
                )
        conn.commit()
    return {"brand_count": len(standards), "alias_count": len(alias_rows)}


def _fetch_score_rows() -> list[dict[str, Any]]:
    with _connect_feature(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    w.formula_id, w.source_id, w.product_key, w.brand, w.product_name,
                    w.protein_structure_score, w.protein_quality_score,
                    w.fat_regulation_score, w.fat_score,
                    w.omega_imbalance_score, w.p_total_score, w.p_buffer,
                    w.q_feed, w.q_scfa, w.q_total_score, w.starch_burden_score,
                    (
                        SELECT protein_score FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS protein_score,
                    (
                        SELECT carb_score FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS carb_score,
                    (
                        SELECT fiber_score FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS fiber_score,
                    (
                        SELECT fat_score FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS sku_fat_score,
                    (
                        SELECT prebiotic_score FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS prebiotic_score,
                    (
                        SELECT antioxidant_score FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS antioxidant_score,
                    (
                        SELECT p_buffer FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS sku_p_buffer,
                    (
                        SELECT q_feed FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS sku_q_feed,
                    (
                        SELECT q_scfa FROM {SKU_FEATURE_TABLE}
                        WHERE (formula_id = w.formula_id)
                           OR (formula_id IS NULL AND sku_id = w.product_key)
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS sku_q_scfa,
                    (
                        SELECT current_pool_risk_level
                        FROM {RISK_RESULT_TABLE}
                        WHERE ((formula_id = w.formula_id)
                            OR (formula_id IS NULL AND sku_id = w.product_key))
                          AND score_model_version LIKE 'BLACK_CHIN%%'
                        ORDER BY calculated_at DESC
                        LIMIT 1
                    ) AS black_chin_risk_level,
                    (
                        SELECT current_pool_risk_level
                        FROM {RISK_RESULT_TABLE}
                        WHERE ((formula_id = w.formula_id)
                            OR (formula_id IS NULL AND sku_id = w.product_key))
                          AND score_model_version LIKE 'SOFT_STOOL%%'
                        ORDER BY calculated_at DESC
                        LIMIT 1
                    ) AS soft_stool_risk_level
                FROM {SCORE_WIDE_TABLE} w
                WHERE w.product_name IS NOT NULL
                  AND TRIM(w.product_name) <> ''
                ORDER BY w.source_id DESC
                """
            )
            return list(cursor.fetchall() or [])


def _function_row_from_score(row: dict[str, Any]) -> dict[str, Any]:
    fat_burden = normalize_score(row.get("sku_fat_score", row.get("fat_score")), "fat_score")
    q_feed = normalize_score(row.get("sku_q_feed", row.get("q_feed")), "q_feed")
    q_scfa = normalize_score(row.get("sku_q_scfa", row.get("q_scfa")), "q_scfa")
    return {
        "source_id": row.get("source_id"),
        "product_key": row.get("product_key"),
        "brand": row.get("brand"),
        "product_name": row.get("product_name"),
        "base_positioning": "日常口粮",
        "protein_quality_score": normalize_score(row.get("protein_quality_score"), "protein_quality_score"),
        "protein_pressure_score": normalize_score(row.get("protein_score", row.get("protein_structure_score")), "protein_score"),
        "carb_burden_score": normalize_score(row.get("carb_score", row.get("starch_burden_score")), "carb_score"),
        "fat_burden_score": fat_burden,
        "fiber_buffer_score": weighted_avg_valid([
            (normalize_score(row.get("fiber_score", row.get("p_total_score")), "fiber_score"), 0.30),
            (normalize_score(row.get("p_total_score"), "p_total_score"), 0.25),
            (normalize_score(row.get("sku_p_buffer", row.get("p_buffer")), "p_buffer"), 0.45),
        ]),
        "microbiome_support_score": weighted_avg_valid([
            (normalize_score(row.get("prebiotic_score", row.get("q_feed")), "prebiotic_score"), 0.35),
            (q_scfa, 0.45),
            (q_feed, 0.20),
        ]),
        "skin_protection_score": weighted_avg_valid([
            (normalize_score(row.get("antioxidant_score", row.get("fat_regulation_score")), "antioxidant_score"), 0.55),
            (normalize_score(row.get("fat_regulation_score"), "fat_regulation_score"), 0.45),
        ]),
        "omega3_support_score": reverse_score(normalize_score(row.get("omega_imbalance_score"), "omega_imbalance_score") or 50),
        "black_chin_friendly_score": normalize_score(row.get("fat_regulation_score"), "fat_regulation_score"),
        "calorie_density_score": fat_burden,
        "black_chin_risk_level": row.get("black_chin_risk_level") or "暂无",
        "soft_stool_risk_level": row.get("soft_stool_risk_level") or "暂无",
    }


def _catalog_row_from_score(row: dict[str, Any], brand_maps: dict[str, Any]) -> dict[str, Any]:
    brand, brand_info, flags = standardize_brand(row.get("brand"), row.get("product_name"), brand_maps)
    function_result = infer_function_positioning(_function_row_from_score(row))
    product_name = _clean_text(row.get("product_name"))
    product_key = _product_key(brand, product_name)
    return {
        "catalog_key": f"score:{row.get('source_id')}",
        "product_key": product_key,
        "standard_brand": brand,
        "raw_brand": _clean_text(row.get("brand")),
        "product_name": product_name,
        "raw_title": product_name,
        "origin_type": brand_info.get("origin_type") or "待确认",
        "brand_tier": brand_info.get("brand_tier") or "",
        "source": "score_db",
        "source_item_id": None,
        "source_url": None,
        "price": None,
        "price_bucket": "未知",
        "food_taste": None,
        "net_content": None,
        "sold_text": None,
        "main_image_url": None,
        "main_images_json": _json_dumps([]),
        "sku_variants_json": _json_dumps([]),
        "compare_available": 1,
        "score_source_id": row.get("source_id"),
        "function_scores_json": _json_dumps(function_result.get("function_scores") or {}),
        "function_tags_json": _json_dumps(function_result.get("function_tags") or []),
        "warning_tags_json": _json_dumps(function_result.get("warning_tags") or []),
        "function_display_text": function_result.get("display_text") or "",
        "status": "active",
        "quality_flags_json": _json_dumps(flags),
    }


def _iter_taobao_items(taobao_dir: Path | str = DEFAULT_TAOBAO_SKU_DIR) -> list[dict[str, Any]]:
    root = Path(taobao_dir).expanduser()
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("catfood_sku_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in payload.get("items") or []:
            if isinstance(item, dict):
                rows.append({**item, "_source_file": str(path)})
    return rows


def _match_score_row(taobao_item: dict[str, Any], score_rows: list[dict[str, Any]], standard_brand: str) -> dict[str, Any] | None:
    title_compact = _compact(taobao_item.get("product_name") or taobao_item.get("title"))
    if not title_compact:
        return None
    candidates = [row for row in score_rows if _clean_text(row.get("standard_brand")) == standard_brand]
    best: tuple[int, dict[str, Any]] | None = None
    for row in candidates:
        product_compact = _compact(row.get("product_name"))
        if not product_compact:
            continue
        score = 0
        if product_compact in title_compact:
            score = len(product_compact) + 20
        elif title_compact in product_compact:
            score = len(title_compact)
        else:
            product_tokens = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", product_compact))
            title_tokens = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]+", title_compact))
            overlap = product_tokens & title_tokens
            if overlap:
                score = sum(len(token) for token in overlap)
        if score and (best is None or score > best[0]):
            best = (score, row)
    if best and best[0] >= 4:
        return best[1]
    return None


def _catalog_row_from_taobao(
    item: dict[str, Any],
    brand_maps: dict[str, Any],
    score_catalog_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not _is_valid_taobao_item(item):
        return None
    brand, brand_info, flags = standardize_brand(item.get("brand"), item.get("title"), brand_maps)
    matched_score = _match_score_row(item, score_catalog_rows, brand)
    source = "merged" if matched_score else "taobao"
    product_name = _clean_text(matched_score.get("product_name")) if matched_score else _normalize_product_name(item.get("product_name") or item.get("title"))
    product_key = _clean_text(matched_score.get("product_key")) if matched_score else _product_key(brand, product_name)
    price = _parse_price(item.get("price"))
    images = item.get("main_images") if isinstance(item.get("main_images"), list) else []
    quality_flags = list(flags)
    if not matched_score:
        quality_flags.append("score_unmatched")
    return {
        "catalog_key": (matched_score or {}).get("catalog_key") or f"taobao:{_clean_text(item.get('item_id'))}",
        "product_key": product_key,
        "standard_brand": brand,
        "raw_brand": _clean_text(item.get("brand")),
        "product_name": product_name,
        "raw_title": _clean_text(item.get("title")),
        "origin_type": brand_info.get("origin_type") or (matched_score or {}).get("origin_type") or "待确认",
        "brand_tier": brand_info.get("brand_tier") or (matched_score or {}).get("brand_tier") or "",
        "source": source,
        "source_item_id": _clean_text(item.get("item_id")),
        "source_url": _clean_text(item.get("url")),
        "price": price,
        "price_bucket": _price_bucket(price),
        "food_taste": _clean_text(item.get("food_taste")),
        "net_content": _clean_text(item.get("net_content")),
        "sold_text": _clean_text(item.get("sold")),
        "main_image_url": images[0] if images else None,
        "main_images_json": _json_dumps(images),
        "sku_variants_json": _json_dumps(item.get("sku_variants") or []),
        "compare_available": 1 if matched_score else 0,
        "score_source_id": (matched_score or {}).get("score_source_id"),
        "function_scores_json": (matched_score or {}).get("function_scores_json") or _json_dumps({}),
        "function_tags_json": (matched_score or {}).get("function_tags_json") or _json_dumps([]),
        "warning_tags_json": (matched_score or {}).get("warning_tags_json") or _json_dumps([]),
        "function_display_text": (matched_score or {}).get("function_display_text") or "",
        "status": "active",
        "quality_flags_json": _json_dumps(quality_flags),
    }


def _upsert_catalog_rows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {CATALOG_TABLE} (
            catalog_key, product_key, standard_brand, raw_brand, product_name, raw_title,
            origin_type, brand_tier, source, source_item_id, source_url,
            price, price_bucket, food_taste, net_content, sold_text,
            main_image_url, main_images_json, sku_variants_json,
            compare_available, score_source_id,
            function_scores_json, function_tags_json, warning_tags_json, function_display_text,
            status, quality_flags_json
        ) VALUES (
            %(catalog_key)s, %(product_key)s, %(standard_brand)s, %(raw_brand)s, %(product_name)s, %(raw_title)s,
            %(origin_type)s, %(brand_tier)s, %(source)s, %(source_item_id)s, %(source_url)s,
            %(price)s, %(price_bucket)s, %(food_taste)s, %(net_content)s, %(sold_text)s,
            %(main_image_url)s, %(main_images_json)s, %(sku_variants_json)s,
            %(compare_available)s, %(score_source_id)s,
            %(function_scores_json)s, %(function_tags_json)s, %(warning_tags_json)s, %(function_display_text)s,
            %(status)s, %(quality_flags_json)s
        )
        ON DUPLICATE KEY UPDATE
            product_key = VALUES(product_key),
            standard_brand = VALUES(standard_brand),
            raw_brand = VALUES(raw_brand),
            product_name = VALUES(product_name),
            raw_title = VALUES(raw_title),
            origin_type = VALUES(origin_type),
            brand_tier = VALUES(brand_tier),
            source = VALUES(source),
            source_item_id = VALUES(source_item_id),
            source_url = VALUES(source_url),
            price = VALUES(price),
            price_bucket = VALUES(price_bucket),
            food_taste = VALUES(food_taste),
            net_content = VALUES(net_content),
            sold_text = VALUES(sold_text),
            main_image_url = VALUES(main_image_url),
            main_images_json = VALUES(main_images_json),
            sku_variants_json = VALUES(sku_variants_json),
            compare_available = VALUES(compare_available),
            score_source_id = VALUES(score_source_id),
            function_scores_json = VALUES(function_scores_json),
            function_tags_json = VALUES(function_tags_json),
            warning_tags_json = VALUES(warning_tags_json),
            function_display_text = VALUES(function_display_text),
            status = VALUES(status),
            quality_flags_json = VALUES(quality_flags_json)
    """
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(sql, rows)
        conn.commit()
    return len(rows)


def rebuild_product_catalog(
    *,
    brand_excel_path: Path | str = DEFAULT_BRAND_EXCEL_PATH,
    taobao_dir: Path | str = DEFAULT_TAOBAO_SKU_DIR,
    truncate: bool = False,
) -> dict[str, Any]:
    ensure_catalog_tables()
    brand_excel_path = brand_excel_path or DEFAULT_BRAND_EXCEL_PATH
    taobao_dir = taobao_dir or DEFAULT_TAOBAO_SKU_DIR
    brand_maps = load_brand_maps(brand_excel_path)
    brand_counts = upsert_brand_tables(brand_maps)

    if truncate:
        with _connect_feature() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"TRUNCATE TABLE {CATALOG_TABLE}")
            conn.commit()

    score_rows = _fetch_score_rows()
    score_catalog_rows = [_catalog_row_from_score(row, brand_maps) for row in score_rows]
    score_count = _upsert_catalog_rows(score_catalog_rows)

    taobao_items = _iter_taobao_items(taobao_dir)
    taobao_catalog_rows = []
    invalid_taobao_count = 0
    for item in taobao_items:
        row = _catalog_row_from_taobao(item, brand_maps, score_catalog_rows)
        if row is None:
            invalid_taobao_count += 1
            continue
        taobao_catalog_rows.append(row)
    taobao_count = _upsert_catalog_rows(taobao_catalog_rows)

    return {
        "ok": True,
        "brand_excel_path": str(Path(brand_excel_path).expanduser()),
        "taobao_dir": str(Path(taobao_dir).expanduser()),
        "truncate": bool(truncate),
        **brand_counts,
        "score_rows": len(score_rows),
        "score_catalog_rows": score_count,
        "taobao_items": len(taobao_items),
        "taobao_catalog_rows": taobao_count,
        "invalid_taobao_items": invalid_taobao_count,
    }


def list_product_options(
    *,
    q: str = "",
    origin: str = "",
    price_bucket: str = "",
    function_tag: str = "",
    brand: str = "",
    compare_available: str | bool | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    ensure_catalog_tables()
    limit = max(1, min(int(limit or 200), 500))
    where = ["status = 'active'"]
    params: list[Any] = []
    if q:
        like = f"%{q.strip()}%"
        where.append("(standard_brand LIKE %s OR product_name LIKE %s OR raw_title LIKE %s OR food_taste LIKE %s)")
        params.extend([like, like, like, like])
    if brand:
        where.append("standard_brand = %s")
        params.append(brand)
    if origin:
        where.append("origin_type = %s")
        params.append(_origin_filter_value(origin))
    if price_bucket:
        where.append("price_bucket = %s")
        params.append(price_bucket)
    if function_tag:
        where.append("JSON_SEARCH(function_tags_json, 'one', %s) IS NOT NULL")
        params.append(function_tag)
    if compare_available not in (None, ""):
        value = compare_available
        if isinstance(value, str):
            value = value.strip().lower() in {"1", "true", "yes", "available"}
        where.append("compare_available = %s")
        params.append(1 if value else 0)
    params.append(limit)

    with _connect_feature(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM {CATALOG_TABLE}
                WHERE {" AND ".join(where)}
                ORDER BY compare_available DESC,
                         FIELD(source, 'merged', 'score_db', 'taobao'),
                         standard_brand ASC,
                         price IS NULL ASC,
                         price ASC,
                         id DESC
                LIMIT %s
                """,
                params,
            )
            rows = list(cursor.fetchall() or [])
    items = []
    for row in rows:
        function_tags = _json_loads(row.get("function_tags_json"), [])
        warning_tags = _json_loads(row.get("warning_tags_json"), [])
        main_images = _json_loads(row.get("main_images_json"), [])
        label = " ".join(part for part in [row.get("standard_brand"), row.get("product_name")] if _clean_text(part))
        items.append({
            "id": row.get("catalog_key"),
            "catalog_key": row.get("catalog_key"),
            "product_key": row.get("product_key"),
            "label": label,
            "brand": row.get("standard_brand"),
            "raw_brand": row.get("raw_brand"),
            "product_name": row.get("product_name"),
            "raw_title": row.get("raw_title"),
            "origin_type": row.get("origin_type"),
            "brand_tier": row.get("brand_tier"),
            "source": row.get("source"),
            "source_item_id": row.get("source_item_id"),
            "source_url": row.get("source_url"),
            "price": _json_safe(row.get("price")),
            "price_bucket": row.get("price_bucket"),
            "food_taste": row.get("food_taste"),
            "net_content": row.get("net_content"),
            "sold_text": row.get("sold_text"),
            "main_image_url": row.get("main_image_url") or (main_images[0] if main_images else None),
            "main_images": main_images,
            "compare_available": bool(row.get("compare_available")),
            "score_source_id": row.get("score_source_id"),
            "function_tags": function_tags,
            "warning_tags": warning_tags,
            "display_text": row.get("function_display_text"),
            "quality_flags": _json_loads(row.get("quality_flags_json"), []),
        })
    return {"ok": True, "count": len(items), "items": items}
