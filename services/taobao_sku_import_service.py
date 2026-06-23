"""Import raw Taobao cat-food SKU JSON exports into a standalone table."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql

from app_config import get_feature_mysql_config, get_mysql_config
from services.catfood_standardization_service import (
    BRAND_TABLE as STANDARD_BRAND_TABLE,
    init_standardization_db,
)
from services.cat_food_product_catalog_service import (
    DEFAULT_BRAND_EXCEL_PATH,
    load_brand_maps,
)


DEFAULT_TAOBAO_SKU_DIR = Path("/Users/yoghourt/anaconda3/envs/comment_labeler_env/taobao/out_catfood_sku")
DEFAULT_TAOBAO_SKU_HISTORY_DIR = Path("/Users/yoghourt/anaconda3/envs/comment_labeler_env/taobao/out_catfood_sku_history")
TAOBAO_SKU_TABLE = "taobao_catfood_sku_items"
TAOBAO_SKU_BRAND_CLEAN_TABLE = "taobao_catfood_brand_cleaned"


def _connect_feature(autocommit: bool = False):
    cfg = get_feature_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _connect_csv(autocommit: bool = False):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


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


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _first_image(images: Any) -> str:
    if isinstance(images, list) and images:
        return _clean_text(images[0])
    return ""


def _fingerprint(source_file: str, item: dict[str, Any]) -> str:
    parts = [
        source_file,
        _clean_text(item.get("item_id")),
        _clean_text(item.get("url")),
        _clean_text(item.get("title")),
        _clean_text(item.get("product_name")),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def ensure_taobao_sku_table() -> None:
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TAOBAO_SKU_TABLE} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    item_fingerprint CHAR(64) NOT NULL,
                    source_file VARCHAR(512) NOT NULL,
                    source_keyword VARCHAR(255) NULL,
                    file_total_keywords INT NULL,
                    file_total_spu INT NULL,
                    item_id VARCHAR(128) NULL,
                    brand VARCHAR(255) NULL,
                    title VARCHAR(1024) NULL,
                    product_name VARCHAR(512) NULL,
                    price VARCHAR(128) NULL,
                    price_source VARCHAR(255) NULL,
                    food_taste VARCHAR(255) NULL,
                    net_content VARCHAR(255) NULL,
                    sold_text VARCHAR(128) NULL,
                    main_image_url TEXT NULL,
                    main_images_json JSON NULL,
                    sku_variants_json JSON NULL,
                    source_url TEXT NULL,
                    raw_item_json JSON NULL,
                    import_batch_id VARCHAR(64) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_item_fingerprint (item_fingerprint),
                    KEY idx_item_id (item_id),
                    KEY idx_source_keyword (source_keyword),
                    KEY idx_import_batch (import_batch_id),
                    KEY idx_created_at (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        conn.commit()


def ensure_taobao_sku_brand_clean_table() -> None:
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TAOBAO_SKU_BRAND_CLEAN_TABLE} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    standard_brand VARCHAR(255) NOT NULL,
                    brand_id BIGINT NULL,
                    standard_brand_name VARCHAR(255) NOT NULL,
                    raw_brand_examples_json JSON NULL,
                    source_keywords_json JSON NULL,
                    alias_summary TEXT NULL,
                    origin_type VARCHAR(64) NULL,
                    brand_tier VARCHAR(64) NULL,
                    brand_status VARCHAR(32) NULL,
                    brand_remark VARCHAR(1024) NULL,
                    item_count INT NOT NULL DEFAULT 0,
                    valid_price_item_count INT NOT NULL DEFAULT 0,
                    avg_price_per_jin DECIMAL(10,2) NULL,
                    median_price_per_jin DECIMAL(10,2) NULL,
                    min_price_per_jin DECIMAL(10,2) NULL,
                    max_price_per_jin DECIMAL(10,2) NULL,
                    price_band VARCHAR(64) NULL,
                    top1_food_taste VARCHAR(255) NULL,
                    top1_food_taste_sold_count BIGINT NULL,
                    top1_food_taste_item_id VARCHAR(128) NULL,
                    top1_food_taste_title VARCHAR(1024) NULL,
                    brand_flags_json JSON NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_standard_brand (standard_brand),
                    KEY idx_origin_type (origin_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(f"SHOW COLUMNS FROM {TAOBAO_SKU_BRAND_CLEAN_TABLE} LIKE 'price_band'")
            if not cursor.fetchone():
                cursor.execute(
                    f"""
                    ALTER TABLE {TAOBAO_SKU_BRAND_CLEAN_TABLE}
                    ADD COLUMN price_band VARCHAR(64) NULL AFTER max_price_per_jin
                    """
                )
            for column_name, column_def in [
                ("brand_id", "ADD COLUMN brand_id BIGINT NULL AFTER standard_brand"),
                ("standard_brand_name", "ADD COLUMN standard_brand_name VARCHAR(255) NULL AFTER brand_id"),
                ("source_keywords_json", "ADD COLUMN source_keywords_json JSON NULL AFTER raw_brand_examples_json"),
                ("alias_summary", "ADD COLUMN alias_summary TEXT NULL AFTER raw_brand_examples_json"),
                ("brand_status", "ADD COLUMN brand_status VARCHAR(32) NULL AFTER brand_tier"),
                ("brand_remark", "ADD COLUMN brand_remark VARCHAR(1024) NULL AFTER brand_status"),
            ]:
                cursor.execute(f"SHOW COLUMNS FROM {TAOBAO_SKU_BRAND_CLEAN_TABLE} LIKE %s", (column_name,))
                if not cursor.fetchone():
                    cursor.execute(f"ALTER TABLE {TAOBAO_SKU_BRAND_CLEAN_TABLE} {column_def}")
        conn.commit()


def _row_from_item(
    *,
    item: dict[str, Any],
    source_file: str,
    file_total_keywords: int | None,
    file_total_spu: int | None,
    import_batch_id: str,
) -> dict[str, Any]:
    main_images = item.get("main_images") if isinstance(item.get("main_images"), list) else []
    sku_variants = item.get("sku_variants") if isinstance(item.get("sku_variants"), list) else []
    return {
        "item_fingerprint": _fingerprint(source_file, item),
        "source_file": source_file,
        "source_keyword": _clean_text(item.get("_source_keyword")),
        "file_total_keywords": file_total_keywords,
        "file_total_spu": file_total_spu,
        "item_id": _clean_text(item.get("item_id")),
        "brand": _clean_text(item.get("brand")),
        "title": _clean_text(item.get("title")),
        "product_name": _clean_text(item.get("product_name")),
        "price": _clean_text(item.get("price")),
        "price_source": _clean_text(item.get("price_source")),
        "food_taste": _clean_text(item.get("food_taste")),
        "net_content": _clean_text(item.get("net_content")),
        "sold_text": _clean_text(item.get("sold")),
        "main_image_url": _first_image(main_images),
        "main_images_json": _json_dumps(main_images),
        "sku_variants_json": _json_dumps(sku_variants),
        "source_url": _clean_text(item.get("url")),
        "raw_item_json": json.dumps(item, ensure_ascii=False),
        "import_batch_id": import_batch_id,
    }


def _iter_rows(data_dir: Path, import_batch_id: str) -> tuple[list[dict[str, Any]], list[Path], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    loaded_paths: list[Path] = []
    stats = {
        "scanned_files": 0,
        "loaded_files": 0,
        "skipped_files": 0,
        "json_items": 0,
        "skipped_items": 0,
    }
    for path in sorted(data_dir.glob("catfood_sku_*.json")):
        stats["scanned_files"] += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            stats["skipped_files"] += 1
            continue
        stats["loaded_files"] += 1
        loaded_paths.append(path)
        items = payload.get("items") or []
        file_total_keywords = payload.get("total_keywords")
        file_total_spu = payload.get("total_spu")
        for item in items:
            if not isinstance(item, dict):
                stats["skipped_items"] += 1
                continue
            stats["json_items"] += 1
            rows.append(
                _row_from_item(
                    item=item,
                    source_file=str(path),
                    file_total_keywords=file_total_keywords if isinstance(file_total_keywords, int) else None,
                    file_total_spu=file_total_spu if isinstance(file_total_spu, int) else None,
                    import_batch_id=import_batch_id,
                )
            )
    return rows, loaded_paths, stats


def _archive_files(paths: list[Path], history_dir: Path, import_batch_id: str) -> dict[str, Any]:
    if not paths:
        return {
            "archive_enabled": True,
            "archive_dir": str(history_dir),
            "archived_files": 0,
        }
    batch_dir = history_dir / import_batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    archived_files = 0
    for path in paths:
        target = batch_dir / path.name
        if target.exists():
            target = batch_dir / f"{path.stem}_{datetime.now().strftime('%H%M%S%f')}{path.suffix}"
        shutil.move(str(path), str(target))
        archived_files += 1
    return {
        "archive_enabled": True,
        "archive_dir": str(batch_dir),
        "archived_files": archived_files,
    }


def _upsert_rows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {TAOBAO_SKU_TABLE} (
            item_fingerprint, source_file, source_keyword, file_total_keywords, file_total_spu,
            item_id, brand, title, product_name, price, price_source,
            food_taste, net_content, sold_text, main_image_url,
            main_images_json, sku_variants_json, source_url, raw_item_json, import_batch_id
        ) VALUES (
            %(item_fingerprint)s, %(source_file)s, %(source_keyword)s, %(file_total_keywords)s, %(file_total_spu)s,
            %(item_id)s, %(brand)s, %(title)s, %(product_name)s, %(price)s, %(price_source)s,
            %(food_taste)s, %(net_content)s, %(sold_text)s, %(main_image_url)s,
            %(main_images_json)s, %(sku_variants_json)s, %(source_url)s, %(raw_item_json)s, %(import_batch_id)s
        )
        ON DUPLICATE KEY UPDATE
            source_file = VALUES(source_file),
            source_keyword = VALUES(source_keyword),
            file_total_keywords = VALUES(file_total_keywords),
            file_total_spu = VALUES(file_total_spu),
            item_id = VALUES(item_id),
            brand = VALUES(brand),
            title = VALUES(title),
            product_name = VALUES(product_name),
            price = VALUES(price),
            price_source = VALUES(price_source),
            food_taste = VALUES(food_taste),
            net_content = VALUES(net_content),
            sold_text = VALUES(sold_text),
            main_image_url = VALUES(main_image_url),
            main_images_json = VALUES(main_images_json),
            sku_variants_json = VALUES(sku_variants_json),
            source_url = VALUES(source_url),
            raw_item_json = VALUES(raw_item_json),
            import_batch_id = VALUES(import_batch_id)
    """
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(sql, rows)
        conn.commit()
    return len(rows)


def _parse_price(value: Any) -> float | None:
    text = _clean_text(value).replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _weight_match_to_jin(match: re.Match[str]) -> float:
    amount = float(match.group(1))
    unit = match.group(2)
    count = float(match.group(3) or 1)
    if unit in {"kg", "千克", "公斤"}:
        return amount * 2 * count
    if unit in {"g", "克"}:
        return amount / 500 * count
    if unit == "斤":
        return amount * count
    if unit in {"磅", "lb", "lbs"}:
        return amount * 0.907185 * count
    return 0.0


def _parse_net_weight_jin(value: Any, *, mode: str = "first") -> float | None:
    text = _clean_text(value).lower().replace(" ", "")
    if not text:
        return None
    unit_pattern = r"(kg|千克|公斤|g|克|斤|磅|lbs?|lb)"
    matches = list(re.finditer(rf"(\d+(?:\.\d+)?)\s*{unit_pattern}(?:\s*[x\*×]\s*(\d+(?:\.\d+)?))?", text))
    if not matches:
        return None
    has_metric = any(match.group(2) not in {"磅", "lb", "lbs"} for match in matches)
    usable_matches = [
        match
        for match in matches
        if float(match.group(1)) > 0 and not (has_metric and match.group(2) in {"磅", "lb", "lbs"})
    ]
    if not usable_matches:
        return None
    if mode == "first":
        total_jin = _weight_match_to_jin(usable_matches[0])
        return round(total_jin, 4) if total_jin > 0 else None

    total_jin = 0.0
    for match in usable_matches:
        total_jin += _weight_match_to_jin(match)
    return round(total_jin, 4) if total_jin > 0 else None


def _parse_item_weight_jin(item: dict[str, Any]) -> float | None:
    net_content = _clean_text(item.get("net_content"))
    if net_content and "以口味重量为准" not in net_content and not net_content.startswith("0g"):
        weight = _parse_net_weight_jin(net_content, mode="first")
        if weight:
            return weight
    for field, mode in (("food_taste", "sum"), ("title", "first"), ("product_name", "first"), ("net_content", "first")):
        weight = _parse_net_weight_jin(item.get(field), mode=mode)
        if weight:
            return weight
    return None


def _price_band(min_price: float | None, max_price: float | None) -> str:
    if min_price is None or max_price is None:
        return "未知"
    min_text = f"{min_price:.2f}".rstrip("0").rstrip(".")
    max_text = f"{max_price:.2f}".rstrip("0").rstrip(".")
    if min_text == max_text:
        return f"{min_text}元/斤"
    return f"{min_text}-{max_text}元/斤"


def _price_bucket_from_value(price_per_jin: Any) -> str:
    try:
        price = float(price_per_jin)
    except (TypeError, ValueError):
        return "未知"
    if price < 50:
        return "<50"
    if price < 80:
        return "50-80"
    return "80以上"


def _parse_sold_count(value: Any) -> int:
    text = _clean_text(value).replace(",", "")
    if not text:
        return 0
    match = re.search(r"(\d+(?:\.\d+)?)\s*(万)?\+?", text)
    if not match:
        return 0
    count = float(match.group(1))
    if match.group(2) == "万":
        count *= 10000
    return int(count)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def _round_optional(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _brand_id_value(value: Any) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _load_excel_brand_maps() -> dict[str, Any]:
    return load_brand_maps(DEFAULT_BRAND_EXCEL_PATH)


def _brand_compact(value: Any) -> str:
    text = _clean_text(value).lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[【】\[\]()（）:：,，。/\\+|｜_\-]+", "", text)
    return text


def _fetch_sku_items() -> list[dict[str, Any]]:
    ensure_taobao_sku_table()
    with _connect_feature(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, item_id, source_keyword, brand, title, product_name, price, food_taste, net_content, sold_text
                FROM {TAOBAO_SKU_TABLE}
                """
            )
            return list(cursor.fetchall() or [])


def _resolve_excel_brand(item: dict[str, Any], brand_maps: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    standard = brand_maps.get("standard") or {}
    alias_to_brand = brand_maps.get("alias_to_brand") or {}
    compact = _brand_compact(item.get("source_keyword"))
    if not compact:
        return None
    if compact in alias_to_brand:
        brand = alias_to_brand[compact]
        return brand, standard.get(brand, {})
    for alias_compact, brand in alias_to_brand.items():
        if alias_compact and alias_compact in compact:
            return brand, standard.get(brand, {})
    return None


def _brand_summary_rows(items: list[dict[str, Any]], brand_maps: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    grouped: dict[str, dict[str, Any]] = {}
    dropped_items = 0
    for item in items:
        resolved_brand = _resolve_excel_brand(item, brand_maps)
        if not resolved_brand:
            dropped_items += 1
            continue
        standard_brand, brand_master = resolved_brand
        price = _parse_price(item.get("price"))
        net_weight_jin = _parse_item_weight_jin(item)
        price_per_jin = price / net_weight_jin if price is not None and net_weight_jin else None
        sold_count = _parse_sold_count(item.get("sold_text"))
        group = grouped.setdefault(
            standard_brand,
            {
                "standard_brand": standard_brand,
                "brand_id": _brand_id_value(brand_master.get("brand_id")),
                "standard_brand_name": brand_master.get("standard_brand") or standard_brand,
                "alias_summary": " / ".join(str(alias) for alias in (brand_master.get("aliases") or []) if _clean_text(alias)),
                "origin_type": brand_master.get("origin_type") or "待确认",
                "brand_tier": brand_master.get("brand_tier") or "",
                "brand_status": brand_master.get("status") or "",
                "brand_remark": brand_master.get("remark") or "",
                "raw_brands": [],
                "source_keywords": [],
                "flags": [],
                "item_count": 0,
                "prices": [],
                "top_item": None,
            },
        )
        group["item_count"] += 1
        raw_brand = _clean_text(item.get("brand"))
        if raw_brand and raw_brand not in group["raw_brands"]:
            group["raw_brands"].append(raw_brand)
        source_keyword = _clean_text(item.get("source_keyword"))
        if source_keyword and source_keyword not in group["source_keywords"]:
            group["source_keywords"].append(source_keyword)
        if price_per_jin is not None:
            group["prices"].append(price_per_jin)
        taste = _clean_text(item.get("food_taste"))
        current_top = group["top_item"]
        if taste and (current_top is None or sold_count > current_top["sold_count"]):
            group["top_item"] = {
                "food_taste": taste,
                "sold_count": sold_count,
                "item_id": _clean_text(item.get("item_id")),
                "title": _clean_text(item.get("title")),
            }

    rows = []
    for group in grouped.values():
        prices = group["prices"]
        avg_price = sum(prices) / len(prices) if prices else None
        median_price = _median(prices)
        min_price = _round_optional(min(prices) if prices else None)
        max_price = _round_optional(max(prices) if prices else None)
        top_item = group["top_item"] or {}
        rows.append(
            {
                "standard_brand": group["standard_brand"],
                "brand_id": group["brand_id"],
                "standard_brand_name": group["standard_brand_name"],
                "raw_brand_examples_json": _json_dumps(group["raw_brands"][:10]),
                "source_keywords_json": _json_dumps(group["source_keywords"][:10]),
                "alias_summary": group["alias_summary"],
                "origin_type": group["origin_type"],
                "brand_tier": group["brand_tier"],
                "brand_status": group["brand_status"],
                "brand_remark": group["brand_remark"],
                "item_count": group["item_count"],
                "valid_price_item_count": len(prices),
                "avg_price_per_jin": _round_optional(avg_price),
                "median_price_per_jin": _round_optional(median_price),
                "min_price_per_jin": min_price,
                "max_price_per_jin": max_price,
                "price_band": _price_band(min_price, max_price),
                "top1_food_taste": top_item.get("food_taste") or "",
                "top1_food_taste_sold_count": top_item.get("sold_count"),
                "top1_food_taste_item_id": top_item.get("item_id") or "",
                "top1_food_taste_title": top_item.get("title") or "",
                "brand_flags_json": _json_dumps(group["flags"]),
            }
        )
    return rows, dropped_items


def _upsert_brand_summary_rows(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {TAOBAO_SKU_BRAND_CLEAN_TABLE} (
            standard_brand, brand_id, standard_brand_name, raw_brand_examples_json, source_keywords_json, alias_summary,
            origin_type, brand_tier, brand_status, brand_remark,
            item_count, valid_price_item_count, avg_price_per_jin, median_price_per_jin,
            min_price_per_jin, max_price_per_jin, price_band,
            top1_food_taste, top1_food_taste_sold_count, top1_food_taste_item_id, top1_food_taste_title,
            brand_flags_json
        ) VALUES (
            %(standard_brand)s, %(brand_id)s, %(standard_brand_name)s, %(raw_brand_examples_json)s, %(source_keywords_json)s, %(alias_summary)s,
            %(origin_type)s, %(brand_tier)s, %(brand_status)s, %(brand_remark)s,
            %(item_count)s, %(valid_price_item_count)s, %(avg_price_per_jin)s, %(median_price_per_jin)s,
            %(min_price_per_jin)s, %(max_price_per_jin)s, %(price_band)s,
            %(top1_food_taste)s, %(top1_food_taste_sold_count)s, %(top1_food_taste_item_id)s, %(top1_food_taste_title)s,
            %(brand_flags_json)s
        )
        ON DUPLICATE KEY UPDATE
            brand_id = VALUES(brand_id),
            standard_brand_name = VALUES(standard_brand_name),
            raw_brand_examples_json = VALUES(raw_brand_examples_json),
            source_keywords_json = VALUES(source_keywords_json),
            alias_summary = VALUES(alias_summary),
            origin_type = VALUES(origin_type),
            brand_tier = VALUES(brand_tier),
            brand_status = VALUES(brand_status),
            brand_remark = VALUES(brand_remark),
            item_count = VALUES(item_count),
            valid_price_item_count = VALUES(valid_price_item_count),
            avg_price_per_jin = VALUES(avg_price_per_jin),
            median_price_per_jin = VALUES(median_price_per_jin),
            min_price_per_jin = VALUES(min_price_per_jin),
            max_price_per_jin = VALUES(max_price_per_jin),
            price_band = VALUES(price_band),
            top1_food_taste = VALUES(top1_food_taste),
            top1_food_taste_sold_count = VALUES(top1_food_taste_sold_count),
            top1_food_taste_item_id = VALUES(top1_food_taste_item_id),
            top1_food_taste_title = VALUES(top1_food_taste_title),
            brand_flags_json = VALUES(brand_flags_json)
    """
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.executemany(sql, rows)
        conn.commit()
    return len(rows)


def _replace_brand_summary_rows(rows: list[dict[str, Any]]) -> int:
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"DELETE FROM {TAOBAO_SKU_BRAND_CLEAN_TABLE}")
        conn.commit()
    return _upsert_brand_summary_rows(rows)


def sync_brand_price_ranges_to_standard_master(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Update the canonical brand master with Taobao price ranges by brand_id."""
    init_standardization_db()
    matched = 0
    skipped = 0
    with _connect_csv() as conn:
        with conn.cursor() as cursor:
            for row in rows:
                brand_id = row.get("brand_id")
                if brand_id in (None, ""):
                    skipped += 1
                    continue
                cursor.execute(
                    f"""
                    UPDATE `{STANDARD_BRAND_TABLE}`
                    SET min_price_per_jin = %s,
                        max_price_per_jin = %s,
                        price_band = %s,
                        updated_at = NOW()
                    WHERE brand_id = %s
                    """,
                    (
                        row.get("min_price_per_jin"),
                        row.get("max_price_per_jin"),
                        row.get("price_band"),
                        int(brand_id),
                    ),
                )
                if cursor.rowcount:
                    matched += 1
                else:
                    skipped += 1
        conn.commit()
    return {"standard_brand_price_updated": matched, "standard_brand_price_skipped": skipped}


def clean_taobao_sku_brands() -> dict[str, Any]:
    ensure_taobao_sku_table()
    ensure_taobao_sku_brand_clean_table()
    brand_maps = _load_excel_brand_maps()
    items = _fetch_sku_items()
    rows, dropped_items = _brand_summary_rows(items, brand_maps)
    cleaned_rows = _replace_brand_summary_rows(rows)
    price_sync = sync_brand_price_ranges_to_standard_master(rows)
    return {
        "ok": True,
        "source_table": TAOBAO_SKU_TABLE,
        "target_table": TAOBAO_SKU_BRAND_CLEAN_TABLE,
        "source_items": len(items),
        "matched_source_items": sum(int(row.get("item_count") or 0) for row in rows),
        "dropped_non_excel_brand_items": dropped_items,
        "cleaned_brand_rows": cleaned_rows,
        **price_sync,
        "items": [_json_safe(row) for row in rows],
    }


def list_brand_cleaned_options(
    *,
    q: str = "",
    origin: str = "",
    price_bucket: str = "",
    function_tag: str = "",
    brand: str = "",
    compare_available: str | bool | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    ensure_taobao_sku_brand_clean_table()
    limit = max(1, min(int(limit or 200), 500))
    where = []
    params: list[Any] = []
    if q:
        like = f"%{q.strip()}%"
        where.append("(standard_brand LIKE %s OR standard_brand_name LIKE %s OR alias_summary LIKE %s OR top1_food_taste LIKE %s)")
        params.extend([like, like, like, like])
    if brand:
        where.append("standard_brand = %s")
        params.append(brand)
    if origin:
        where.append("origin_type = %s")
        params.append(origin)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)

    with _connect_feature(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM {TAOBAO_SKU_BRAND_CLEAN_TABLE}
                {where_sql}
                ORDER BY standard_brand ASC
                LIMIT %s
                """,
                params,
            )
            rows = list(cursor.fetchall() or [])

    items = []
    for row in rows:
        avg_price = _json_safe(row.get("avg_price_per_jin"))
        row_price_bucket = _price_bucket_from_value(avg_price)
        if price_bucket and row_price_bucket != price_bucket:
            continue
        if compare_available not in (None, ""):
            value = compare_available
            if isinstance(value, str):
                value = value.strip().lower() in {"1", "true", "yes", "available"}
            if not value:
                continue
        standard_brand = row.get("standard_brand")
        label = row.get("standard_brand_name") or standard_brand
        source_keywords = _json_loads(row.get("source_keywords_json"), [])
        raw_brands = _json_loads(row.get("raw_brand_examples_json"), [])
        price_band = row.get("price_band")
        items.append(
            {
                "id": f"brand_cleaned:{standard_brand}",
                "catalog_key": f"brand_cleaned:{standard_brand}",
                "product_key": standard_brand,
                "label": label,
                "brand": standard_brand,
                "raw_brand": ", ".join(raw_brands) if isinstance(raw_brands, list) else "",
                "product_name": label,
                "raw_title": label,
                "origin_type": row.get("origin_type"),
                "brand_tier": row.get("brand_tier"),
                "source": "taobao_brand_cleaned",
                "source_item_id": row.get("top1_food_taste_item_id"),
                "source_url": None,
                "price": avg_price,
                "price_bucket": row_price_bucket,
                "price_band": price_band,
                "food_taste": row.get("top1_food_taste"),
                "net_content": price_band,
                "sold_text": str(row.get("top1_food_taste_sold_count") or ""),
                "main_image_url": None,
                "main_images": [],
                "compare_available": True,
                "score_source_id": None,
                "function_tags": [],
                "warning_tags": [],
                "display_text": " / ".join(item for item in [price_band, row.get("top1_food_taste")] if item),
                "quality_flags": [],
                "source_keywords": source_keywords if isinstance(source_keywords, list) else [],
            }
        )
    return {"ok": True, "count": len(items), "items": items}


def _fetch_brand_cleaned_rows() -> list[dict[str, Any]]:
    ensure_taobao_sku_brand_clean_table()
    with _connect_feature(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT standard_brand, standard_brand_name, raw_brand_examples_json, source_keywords_json,
                       origin_type, brand_tier, avg_price_per_jin, price_band,
                       top1_food_taste, top1_food_taste_sold_count
                FROM {TAOBAO_SKU_BRAND_CLEAN_TABLE}
                ORDER BY standard_brand ASC
                """
            )
            return list(cursor.fetchall() or [])


def _resolve_cleaned_brand_for_ocr(row: dict[str, Any], brand_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    haystack = _brand_compact(" ".join(_clean_text(row.get(field)) for field in ("brand", "product_name", "image_name")))
    if not haystack:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for brand_row in brand_rows:
        candidates = [
            brand_row.get("standard_brand"),
            brand_row.get("standard_brand_name"),
            *(_json_loads(brand_row.get("source_keywords_json"), []) or []),
            *(_json_loads(brand_row.get("raw_brand_examples_json"), []) or []),
        ]
        for candidate in candidates:
            compact = _brand_compact(candidate)
            if not compact:
                continue
            score = 0
            if haystack == compact:
                score = len(compact) + 100
            elif compact in haystack:
                score = len(compact)
            if score and (best is None or score > best[0]):
                best = (score, brand_row)
    return best[1] if best else None


def _fetch_ocr_parsed_rows(limit: int, q: str = "") -> list[dict[str, Any]]:
    normalized_query = _clean_text(q)
    query_like = f"%{normalized_query}%" if normalized_query else ""
    with _connect_csv(autocommit=True) as conn:
        with conn.cursor() as cursor:
            query_sql = ""
            params: list[Any] = []
            if query_like:
                query_sql = """
                  AND (
                    brand LIKE %s
                    OR product_name LIKE %s
                    OR image_name LIKE %s
                    OR CONCAT(COALESCE(brand, ''), ' ', COALESCE(product_name, '')) LIKE %s
                  )
                """
                params.extend([query_like, query_like, query_like, query_like])
            params.append(max(limit * 5, limit))
            cursor.execute(
                f"""
                SELECT id, source_id, image_name, image_path, brand, product_name, ingredient_composition
                FROM catfood_ingredient_ocr_parsed
                WHERE ingredient_composition IS NOT NULL
                  AND TRIM(ingredient_composition) <> ''
                  {query_sql}
                ORDER BY id DESC
                LIMIT %s
                """,
                params,
            )
            return list(cursor.fetchall() or [])


def list_ocr_product_options_with_brand_mapping(
    *,
    q: str = "",
    origin: str = "",
    price_bucket: str = "",
    function_tag: str = "",
    brand: str = "",
    compare_available: str | bool | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 200), 500))
    brand_rows = _fetch_brand_cleaned_rows()
    parsed_rows = _fetch_ocr_parsed_rows(limit, q=q)
    normalized_query = q.strip().lower()
    items = []
    seen = set()
    for row in parsed_rows:
        brand_row = _resolve_cleaned_brand_for_ocr(row, brand_rows)
        raw_brand = _clean_text(row.get("brand"))
        standard_brand = _clean_text((brand_row or {}).get("standard_brand")) or raw_brand or "待确认"
        if brand and standard_brand != brand:
            continue
        if origin and _clean_text((brand_row or {}).get("origin_type")) != origin:
            continue
        avg_price = _json_safe((brand_row or {}).get("avg_price_per_jin"))
        row_price_bucket = _price_bucket_from_value(avg_price)
        if price_bucket and row_price_bucket != price_bucket:
            continue
        product_name = _clean_text(row.get("product_name")) or _clean_text(row.get("image_name")) or f"source:{row.get('source_id') or row.get('id')}"
        label = " ".join(part for part in [standard_brand, product_name] if part)
        haystack = " ".join(
            _clean_text(value)
            for value in [
                label,
                standard_brand,
                row.get("brand"),
                row.get("product_name"),
                row.get("image_name"),
                row.get("ingredient_composition"),
                (brand_row or {}).get("top1_food_taste"),
            ]
        ).lower()
        if normalized_query and normalized_query not in haystack:
            continue
        dedupe_key = row.get("source_id") or row.get("id") or label
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        source_id = row.get("source_id")
        price_band = (brand_row or {}).get("price_band")
        items.append(
            {
                "id": f"ocr:{source_id or row.get('id')}",
                "catalog_key": f"ocr:{source_id or row.get('id')}",
                "product_key": f"{standard_brand}||{product_name}",
                "label": label,
                "brand": standard_brand,
                "raw_brand": row.get("brand"),
                "product_name": product_name,
                "raw_title": row.get("image_name") or product_name,
                "origin_type": (brand_row or {}).get("origin_type"),
                "brand_tier": (brand_row or {}).get("brand_tier"),
                "source": "ocr_parsed_brand_mapped",
                "source_item_id": str(source_id or ""),
                "source_url": None,
                "price": avg_price,
                "price_bucket": row_price_bucket,
                "price_band": price_band,
                "food_taste": (brand_row or {}).get("top1_food_taste"),
                "net_content": price_band,
                "sold_text": str((brand_row or {}).get("top1_food_taste_sold_count") or ""),
                "main_image_url": None,
                "main_images": [],
                "compare_available": True,
                "score_source_id": source_id,
                "function_tags": [],
                "warning_tags": [],
                "display_text": row.get("ingredient_composition"),
                "quality_flags": [],
            }
        )
        if len(items) >= limit:
            break
    return {"ok": True, "count": len(items), "items": items}


def import_taobao_sku_items(
    *,
    data_dir: str | Path = DEFAULT_TAOBAO_SKU_DIR,
    history_dir: str | Path = DEFAULT_TAOBAO_SKU_HISTORY_DIR,
    truncate: bool = False,
    archive: bool = True,
) -> dict[str, Any]:
    ensure_taobao_sku_table()
    root = Path(data_dir or DEFAULT_TAOBAO_SKU_DIR).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"淘宝 SKU 目录不存在: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"淘宝 SKU 路径不是目录: {root}")
    archive_root = Path(history_dir or DEFAULT_TAOBAO_SKU_HISTORY_DIR).expanduser()

    if truncate:
        with _connect_feature() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"TRUNCATE TABLE {TAOBAO_SKU_TABLE}")
            conn.commit()

    import_batch_id = datetime.now().strftime("%Y%m%d%H%M%S")
    rows, loaded_paths, stats = _iter_rows(root, import_batch_id)
    imported_rows = _upsert_rows(rows)
    archive_result = (
        _archive_files(loaded_paths, archive_root, import_batch_id)
        if archive
        else {"archive_enabled": False, "archive_dir": str(archive_root), "archived_files": 0}
    )
    return {
        "ok": True,
        "table": TAOBAO_SKU_TABLE,
        "data_dir": str(root),
        "truncate": bool(truncate),
        "import_batch_id": import_batch_id,
        "prepared_rows": len(rows),
        "imported_rows": imported_rows,
        **archive_result,
        **stats,
    }
