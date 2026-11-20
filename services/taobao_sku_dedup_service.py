"""Taobao cat-food SKU-level representative dedup.

按 SKU（即采集关键词 source_keyword，如「皇家 DR25」）分组，从明细表
taobao_catfood_sku_items 中为每个 SKU 挑出销量最高的一条商品作为代表，
写入独立表 taobao_catfood_sku_representative。原明细表不做任何删改。

同时为每个代表 SKU 绑定配方画像 ID：从 catfood_formula_profile 按
「brand + product_name」归一化匹配；一个 SKU 命中多个配方时用稳定哈希
选一个（既分散又幂等），匹配不到则留 NULL。

销量解析复用 taobao_sku_import_service._parse_sold_count（支持「10万+」等）。
销量并列时按 source row id 升序取最早导入的一条，保证结果稳定可复现。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app_config import get_feature_mysql_config
from services.taobao_sku_import_service import (
    TAOBAO_SKU_TABLE,
    _connect_feature,
    _parse_sold_count,
)

SKU_REPRESENTATIVE_TABLE = "taobao_catfood_sku_representative"
FORMULA_PROFILE_TABLE = "catfood_formula_profile"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def _norm(value: str) -> str:
    """归一化检索词：去所有空白 + 小写，与 targets 子命令一致。"""
    return re.sub(r"\s+", "", value).lower()


def _to_text(value: Any) -> str | None:
    """JSON 列在 pymysql 下通常已是字符串；兼容 list/dict 兜底。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def ensure_sku_representative_table() -> None:
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SKU_REPRESENTATIVE_TABLE} (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    sku VARCHAR(255) NOT NULL,
                    formula_id BIGINT NULL,
                    source_item_id VARCHAR(128) NULL,
                    brand VARCHAR(255) NULL,
                    title VARCHAR(1024) NULL,
                    product_name VARCHAR(512) NULL,
                    price VARCHAR(128) NULL,
                    price_source VARCHAR(255) NULL,
                    food_taste VARCHAR(255) NULL,
                    net_content VARCHAR(255) NULL,
                    sold_text VARCHAR(128) NULL,
                    sold_count BIGINT NOT NULL DEFAULT 0,
                    candidate_count INT NOT NULL DEFAULT 0,
                    main_image_url TEXT NULL,
                    main_images_json TEXT NULL,
                    sku_variants_json TEXT NULL,
                    source_url TEXT NULL,
                    raw_item_json LONGTEXT NULL,
                    source_file VARCHAR(512) NULL,
                    import_batch_id VARCHAR(64) NULL,
                    source_row_id BIGINT UNSIGNED NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_sku (sku),
                    KEY idx_formula_id (formula_id),
                    KEY idx_source_item_id (source_item_id),
                    KEY idx_sold_count (sold_count)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 向后兼容：已存在的表补 formula_id 列与索引
            cursor.execute(
                f"SHOW COLUMNS FROM {SKU_REPRESENTATIVE_TABLE} LIKE 'formula_id'")
            if not cursor.fetchone():
                cursor.execute(
                    f"ALTER TABLE {SKU_REPRESENTATIVE_TABLE} "
                    f"ADD COLUMN formula_id BIGINT NULL AFTER sku, "
                    f"ADD KEY idx_formula_id (formula_id)"
                )
        conn.commit()


def _fetch_formula_map() -> dict[str, list[int]]:
    """检索词(归一化) -> 该词对应的 formula_id 列表。"""
    mapping: dict[str, list[int]] = {}
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT formula_id, brand, product_name FROM {FORMULA_PROFILE_TABLE} "
                f"WHERE formula_id IS NOT NULL AND brand IS NOT NULL AND product_name IS NOT NULL"
            )
            for row in cursor.fetchall():
                kw = _norm(f"{row['brand']} {row['product_name']}")
                if not kw:
                    continue
                fid = int(row["formula_id"])
                bucket = mapping.setdefault(kw, [])
                if fid not in bucket:
                    bucket.append(fid)
    for bucket in mapping.values():
        bucket.sort()
    return mapping


def _pick_formula_id(sku: str, mapping: dict[str, list[int]]) -> int | None:
    """按 SKU 归一化匹配配方；命中多个时用稳定哈希选一个（幂等）。"""
    ids = mapping.get(_norm(sku))
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    digest = hashlib.md5(_norm(sku).encode("utf-8")).hexdigest()
    return ids[int(digest, 16) % len(ids)]


def _fetch_source_items() -> list[dict[str, Any]]:
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT id, source_file, source_keyword, item_id, brand, title, product_name, "
                f"price, price_source, food_taste, net_content, sold_text, main_image_url, "
                f"main_images_json, sku_variants_json, source_url, raw_item_json, import_batch_id "
                f"FROM {TAOBAO_SKU_TABLE}"
            )
            return list(cursor.fetchall())


def build_representative_rows(
    items: list[dict[str, Any]],
    formula_map: dict[str, list[int]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """按 SKU 分组，每组取销量最高的一条并绑定 formula_id。
    返回 (rows, 无SKU被跳过的行数)。formula_map 为 None 时不绑定。"""
    if formula_map is None:
        formula_map = {}
    groups: dict[str, list[dict[str, Any]]] = {}
    skipped_no_sku = 0
    for item in items:
        sku = _clean(item.get("source_keyword"))
        if not sku:
            skipped_no_sku += 1
            continue
        groups.setdefault(sku, []).append(item)

    rows: list[dict[str, Any]] = []
    for sku, candidates in groups.items():
        ranked = sorted(
            candidates,
            key=lambda r: (-_parse_sold_count(r.get("sold_text")), int(r.get("id") or 0)),
        )
        top = ranked[0]
        rows.append(
            {
                "sku": sku,
                "formula_id": _pick_formula_id(sku, formula_map),
                "source_item_id": _clean(top.get("item_id")),
                "brand": _clean(top.get("brand")),
                "title": _clean(top.get("title")),
                "product_name": _clean(top.get("product_name")),
                "price": _clean(top.get("price")),
                "price_source": _clean(top.get("price_source")),
                "food_taste": _clean(top.get("food_taste")),
                "net_content": _clean(top.get("net_content")),
                "sold_text": _clean(top.get("sold_text")),
                "sold_count": _parse_sold_count(top.get("sold_text")),
                "candidate_count": len(candidates),
                "main_image_url": _clean(top.get("main_image_url")),
                "main_images_json": _to_text(top.get("main_images_json")),
                "sku_variants_json": _to_text(top.get("sku_variants_json")),
                "source_url": _clean(top.get("source_url")),
                "raw_item_json": _to_text(top.get("raw_item_json")),
                "source_file": _clean(top.get("source_file")),
                "import_batch_id": _clean(top.get("import_batch_id")),
                "source_row_id": top.get("id"),
            }
        )
    rows.sort(key=lambda r: r["sku"])
    return rows, skipped_no_sku


_INSERT_SQL = f"""
    INSERT INTO {SKU_REPRESENTATIVE_TABLE} (
        sku, formula_id, source_item_id, brand, title, product_name, price, price_source,
        food_taste, net_content, sold_text, sold_count, candidate_count,
        main_image_url, main_images_json, sku_variants_json, source_url,
        raw_item_json, source_file, import_batch_id, source_row_id
    ) VALUES (
        %(sku)s, %(formula_id)s, %(source_item_id)s, %(brand)s, %(title)s, %(product_name)s, %(price)s, %(price_source)s,
        %(food_taste)s, %(net_content)s, %(sold_text)s, %(sold_count)s, %(candidate_count)s,
        %(main_image_url)s, %(main_images_json)s, %(sku_variants_json)s, %(source_url)s,
        %(raw_item_json)s, %(source_file)s, %(import_batch_id)s, %(source_row_id)s
    )
    ON DUPLICATE KEY UPDATE
        formula_id = VALUES(formula_id),
        source_item_id = VALUES(source_item_id),
        brand = VALUES(brand),
        title = VALUES(title),
        product_name = VALUES(product_name),
        price = VALUES(price),
        price_source = VALUES(price_source),
        food_taste = VALUES(food_taste),
        net_content = VALUES(net_content),
        sold_text = VALUES(sold_text),
        sold_count = VALUES(sold_count),
        candidate_count = VALUES(candidate_count),
        main_image_url = VALUES(main_image_url),
        main_images_json = VALUES(main_images_json),
        sku_variants_json = VALUES(sku_variants_json),
        source_url = VALUES(source_url),
        raw_item_json = VALUES(raw_item_json),
        source_file = VALUES(source_file),
        import_batch_id = VALUES(import_batch_id),
        source_row_id = VALUES(source_row_id)
"""


def rebuild_sku_representatives() -> dict[str, Any]:
    """全量重建 SKU 代表表（先清空再写入）并绑定 formula_id，返回摘要。"""
    ensure_sku_representative_table()
    formula_map = _fetch_formula_map()
    items = _fetch_source_items()
    rows, skipped = build_representative_rows(items, formula_map)

    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"DELETE FROM {SKU_REPRESENTATIVE_TABLE}")
            if rows:
                cursor.executemany(_INSERT_SQL, rows)
        conn.commit()

    bound = sum(1 for r in rows if r["formula_id"] is not None)
    return {
        "ok": True,
        "source_table": TAOBAO_SKU_TABLE,
        "target_table": SKU_REPRESENTATIVE_TABLE,
        "formula_source_table": FORMULA_PROFILE_TABLE,
        "source_rows": len(items),
        "sku_count": len(rows),
        "representative_rows": len(rows),
        "formula_bound": bound,
        "formula_unbound": len(rows) - bound,
        "skipped_no_sku_rows": skipped,
        "dedup_removed_rows": len(items) - skipped - len(rows),
    }


def list_sku_representatives() -> list[dict[str, Any]]:
    """列出当前 SKU 代表表全部行，便于核对。"""
    ensure_sku_representative_table()
    with _connect_feature() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT id, sku, formula_id, source_item_id, brand, product_name, price, sold_text, "
                f"sold_count, candidate_count, source_row_id, import_batch_id "
                f"FROM {SKU_REPRESENTATIVE_TABLE} ORDER BY sku"
            )
            return list(cursor.fetchall())
