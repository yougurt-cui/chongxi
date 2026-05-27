from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

TABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")

NAME_LABELS = ["产品名称", "商品名称", "品名", "名称", "产品名", "name"]
INGREDIENT_LABELS = sorted([
    "组成",
    "原料组成",
    "配料组成",
    "配料表",
    "原料表",
    "成分表",
    "配方成分",
    "配料",
    "原料",
    "成分",
    "原材料",
    "主要原料、添加物",
    "主要原料",
    "原料、添加物",
], key=len, reverse=True)
INGREDIENT_LABELS_FALLBACK = ["ingredients", "ingredient"]
PHONE_LABELS = ["联系电话", "客服热线", "客服电话", "服务电话", "电话", "热线", "phone", "tel"]
IMPORTER_LABELS = ["进口商名称", "进口商", "代理商", "经销商", "总经销", "国内代理", "importer"]
ALL_LABELS = NAME_LABELS + INGREDIENT_LABELS + PHONE_LABELS + IMPORTER_LABELS
END_SECTION_LABELS = [
    "联系电话", "客服热线", "客服电话", "服务电话", "电话",
    "进口商", "进口商名称", "生产商", "制造商", "经销商",
    "地址", "净含量", "净 含 量", "保质期", "保 质 期", "贮存", "适用", "适用对象",
    "喂食", "饲喂", "换粮步骤", "换粮建议", "产品信息", "PRODUCT INFORMATION",
    "88VIP", "顶部", "店铺", "客服", "加入购物车", "立即购买",
]
INGREDIENT_TAIL_LABELS = [
    "添加剂组成", "添加剂", "营养添加剂", "保证成分", "营养成分分析值",
    "产品成分分析保证值", "产品成分分析", "成分分析", "分析保证值", "营养保证",
    "营养成分保证值分析", "营养成分保证值", "营养成分分析", "营养成分表", "营养指标",
    "进口登记证号", "进口产品复核检验报告编号", "检验报告编号",
    "贮存条件及方法", "贮存条件", "贮存方法", "贮存",
    "注意事项", "执行标准", "生产日期", "保质期",
    "全价冻干猫粮", "宠物全价猫粮", "全价猫粮",
    "卡路里含量", "代谢能", "建议喂养指南", "喂养指南", "数据来源",
    "换粮步骤", "换粮建议", "产品信息", "PRODUCT INFORMATION",
    "88VIP", "顶部", "店铺", "客服", "加入购物车", "立即购买",
    "guaranteed analysis", "feeding guide", "feeding guidelines", "calorie content",
]
INGREDIENT_HEAD_LABELS = sorted(
    set(INGREDIENT_LABELS + ["成分表", "原料表", "配方成分"] + INGREDIENT_LABELS_FALLBACK),
    key=len,
    reverse=True,
)
ANALYSIS_START_PATTERNS = [
    "guaranteedanalysis",
    "guaranteed analysis",
    "分析保证值",
    "成分分析",
    "产品成分分析",
    "产品成分分析保证值",
    "营养成分分析",
    "营养成分保证值",
    "营养成分保证值分析",
    "营养成分表",
    "营养指标",
]
ANALYSIS_METRIC_KEYWORDS = [
    "粗蛋白", "粗脂肪", "粗纤维", "粗灰分", "水分", "牛磺酸", "omega-3", "omega-6",
    "维生素", "维他命", "卡路里", "kcal", "cfu", "蛋白質", "脂肪", "碳水化合物",
    "營養素", "营养素", "鈣", "钙", "磷", "鎂", "镁", "鈉", "钠",
]
SUSPICIOUS_INGREDIENT_MARKERS = [
    "分析保证值", "产品成分分析", "营养成分分析", "营养指标", "建议喂养指南",
    "喂养指南", "数据来源", "卡路里含量", "添加剂组成",
]
INGREDIENT_SECTION_HEADINGS = (
    "膳食营养成分",
    "益生菌、益生元及其他有益成分",
    "益生菌益生元及其他有益成分",
    "益生菌、益生元",
    "益生菌益生元",
    "氨基酸及其衍生成分",
    "维生素及其衍生物成分",
    "矿物元素成分",
    "矿物质元素成分",
    "微量元素成分",
)
IMPLICIT_INGREDIENT_KEYWORDS = (
    "肉", "鸡", "鸭", "鱼", "牛", "羊", "兔", "肝", "心", "油", "粉", "米", "玉米",
    "南瓜", "胡萝卜", "甘蓝", "苜蓿", "蔓越莓", "蓝莓", "西兰花", "车前子",
    "卵磷脂", "乳杆菌", "甘露寡糖", "果寡糖", "葡聚糖", "溶菌酶", "丝兰",
    "牛磺酸", "赖氨酸", "蛋氨酸", "维生素", "碳酸钙", "甘氨酸", "磷酸氢钙",
)
IMPLICIT_INGREDIENT_STOP_LINES = (
    "pro",
    "全价猫粮",
    "高含肉量",
)
IMPLICIT_ANALYSIS_METRIC_PATTERNS = [
    r"粗蛋白(?:质)?",
    r"粗脂肪",
    r"粗纤维",
    r"粗灰分",
    r"水分",
    r"总磷",
    r"牛磺酸",
    r"水溶性氯化物(?:\([^)]*\))?",
    r"omega[-\s]?[36]",
    r"钙",
    r"代谢能",
    r"卡路里(?:含量)?",
]
IMPLICIT_ANALYSIS_VALUE_PATTERN = (
    r"(?:[≥≤<>]\s*\d+(?:\.\d+)?|"
    r"\d+(?:\.\d+)?\s*(?:%|mg/kg|g/kg|iu/kg|kcal/kg|kj(?:me)?/100g|kj/kg|cfu/kg))"
)
IMAGE_NAME_BRAND_ALIAS_GROUPS: Sequence[Tuple[str, Sequence[str]]] = (
    ("百利", ("百利", "百丽")),
    ("绿福摩", ("绿福摩", "福摩")),
    ("自然光", ("自然光", "自然光环")),
    ("爱肯拿", ("爱肯拿", "安肯拿")),
    ("鲜朗", ("鲜朗", "鲜郎")),
    ("卫仕", ("卫仕", "卫士")),
    ("宽福", ("宽福", "爱初KUANFU", "KUANFU", "爱初宽福")),
    ("go", ("go", "go!")),
)
IMAGE_NAME_EXTRA_BRANDS = (
    "百利",
    "霸弗",
    "金故",
    "普瑞纳",
    "宽福",
    "猫乐适",
    "宠搭",
)
IMAGE_NAME_GENERIC_PRODUCT_NAMES = {
    "猫粮",
    "配料表",
    "原料表",
    "成分表",
    "原料组成",
    "配料组成",
    "配方成分",
    "猫粮配料表",
    "猫粮原料表",
    "猫粮成分表",
    "宠物全价猫粮",
    "全价猫粮",
}
BRAND_PREFIX_STOP_WORDS = {
    "全价",
    "幼猫",
    "成猫",
    "鲜肉",
    "冻干",
    "烘焙",
    "低敏",
    "无谷",
    "益生菌",
    "高蛋白",
    "宠物全价",
}
UNKNOWN_BRAND_PREFIX = "未知品牌"
UNKNOWN_PRODUCT_PREFIX = "未知产品"
WEAK_PRODUCT_NAME_MARKERS = (
    "保质期",
    "生产日期",
    "有效期",
    "联系电话",
    "电话",
    "地址",
)
PRODUCT_NAME_TITLE_MARKERS = (
    "猫粮",
    "犬粮",
    "主粮",
    "鲜美粮",
    "冻干粮",
    "烘焙粮",
)


@dataclass
class ParseSummary:
    scanned: int
    upserted: int
    source_table: str
    target_table: str
    batch_id: str


@dataclass
class IngredientCleanSummary:
    scanned: int
    updated: int
    brand_updated: int
    product_name_updated: int
    table_name: str
    batch_id: str
    backup_table: Optional[str] = None


def _safe_table(name: str) -> str:
    if not TABLE_RE.fullmatch(name):
        raise ValueError(f"invalid table name: {name}")
    return name


def ensure_parsed_table(engine: Engine, table_name: str = "catfood_ingredient_ocr_parsed") -> None:
    table_name = _safe_table(table_name)
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{table_name}` (
      id BIGINT PRIMARY KEY AUTO_INCREMENT,
      source_id BIGINT NOT NULL,
      image_path VARCHAR(1024) NULL,
      image_name VARCHAR(255) NULL,
      file_sha256 CHAR(64) NULL,
      image_group_key VARCHAR(255) NULL,
      merged_source_ids VARCHAR(1024) NULL,
      brand VARCHAR(255) NULL,
      product_name VARCHAR(512) NULL,
      ingredient_composition LONGTEXT NULL,
      phone VARCHAR(64) NULL,
      importer VARCHAR(255) NULL,
      ocr_text LONGTEXT NULL,
      ocr_json LONGTEXT NULL,
      parse_batch_id VARCHAR(32) NOT NULL,
      parse_ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY uq_source_id (source_id),
      KEY idx_image_group_key (image_group_key),
      KEY idx_brand (brand),
      KEY idx_product_name (product_name),
      KEY idx_phone (phone),
      KEY idx_importer (importer)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
        cols = conn.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).fetchall()
        col_set = {r[0] for r in cols}
        if "image_path" not in col_set:
            conn.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN image_path VARCHAR(1024) NULL AFTER source_id"))
        if "brand" not in col_set:
            conn.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN brand VARCHAR(255) NULL AFTER file_sha256"))
        if "image_group_key" not in col_set:
            conn.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN image_group_key VARCHAR(255) NULL AFTER file_sha256"))
        if "merged_source_ids" not in col_set:
            conn.execute(text(f"ALTER TABLE `{table_name}` ADD COLUMN merged_source_ids VARCHAR(1024) NULL AFTER image_group_key"))
        idx_rows = conn.execute(text(f"SHOW INDEX FROM `{table_name}`")).fetchall()
        idx_names = {r[2] for r in idx_rows}
        if "idx_image_group_key" not in idx_names:
            conn.execute(text(f"ALTER TABLE `{table_name}` ADD INDEX idx_image_group_key (image_group_key)"))
        if "idx_brand" not in idx_names:
            conn.execute(text(f"ALTER TABLE `{table_name}` ADD INDEX idx_brand (brand)"))


def _normalize(s: str) -> str:
    out = (s or "").strip()
    out = out.replace("：", ":")
    out = re.sub(r"[ \t]+", " ", out)
    return out.strip(" \n\r\t;,，。\"'{}[]")


def _normalize_product_text(value: Any) -> str:
    out = str(value or "").strip()
    out = re.sub(r"\s+", " ", out)
    return out


def _normalize_brand_key(value: Any) -> str:
    text_value = _normalize_product_text(value).lower()
    text_value = text_value.replace("（", "(").replace("）", ")")
    text_value = re.sub(r"猫粮$", "", text_value)
    text_value = re.sub(r"品牌$", "", text_value)
    text_value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text_value)
    return text_value


def _load_known_brands(engine: Engine, table_name: str) -> List[str]:
    table_name = _safe_table(table_name)
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT DISTINCT brand
                FROM `{table_name}`
                WHERE brand IS NOT NULL AND TRIM(brand) <> ''
                """
            )
        ).fetchall()
    return [_normalize_product_text(row[0]) for row in rows if _normalize_product_text(row[0])]


def _clean_image_name_stem(image_name: Optional[str]) -> Optional[str]:
    stem = Path(str(image_name or "")).stem.strip()
    if not stem:
        return None
    stem = re.sub(r"[_-]+n\d+_i\d+_\d+$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s*20\d{2}[-_.]\d{2}[-_.]\d{2}\s+\d{1,2}\.\d{2}\.\d{2}$", "", stem)
    stem = re.sub(r"\s*\d{1,2}\.\d{2}\.\d{2}$", "", stem)
    stem = _strip_image_sequence_suffix(stem)
    stem = stem.replace("_", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem or None


def _strip_image_sequence_suffix(stem: str) -> str:
    out = str(stem or "").strip()
    if not out:
        return out
    patterns = (
        r"\s*[\-_ ]第?\d{1,3}(?:页|張|张)?$",
        r"\s*[（(]\d{1,3}[）)]$",
    )
    for pattern in patterns:
        cleaned = re.sub(pattern, "", out, flags=re.IGNORECASE).strip()
        if cleaned and cleaned != out:
            return cleaned
    return out


def _looks_like_camera_filename(stem: Optional[str]) -> bool:
    compact = re.sub(r"[^0-9A-Za-z]+", "", str(stem or ""))
    if not compact:
        return False
    return bool(re.fullmatch(r"(?:IMG\d+|PXL\d+|DSC\d+|\d{10,})", compact, flags=re.IGNORECASE))


def _image_name_group_key(image_name: Optional[str]) -> Optional[str]:
    stem = _clean_image_name_stem(image_name)
    if not stem or _looks_like_camera_filename(stem):
        return None
    return _normalize_brand_key(stem) or None


def _build_image_name_brand_candidates(known_brands: Sequence[str]) -> List[Tuple[str, str, str]]:
    normalized_brands = {_normalize_product_text(brand) for brand in known_brands if _normalize_product_text(brand)}
    candidates: List[Tuple[str, str, str]] = []
    seen = set()

    def add(alias: str, canonical: str) -> None:
        alias_text = _normalize_product_text(alias)
        canonical_text = _normalize_product_text(canonical)
        alias_key = _normalize_brand_key(alias_text)
        if not alias_text or not canonical_text or not alias_key:
            return
        dedupe_key = (alias_text, canonical_text)
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        candidates.append((alias_text, canonical_text, alias_key))

    for default_canonical, aliases in IMAGE_NAME_BRAND_ALIAS_GROUPS:
        canonical = default_canonical
        for alias in (default_canonical, *aliases):
            add(alias, canonical)
        add(canonical, canonical)

    for brand in sorted(normalized_brands, key=lambda value: (len(_normalize_brand_key(value)), len(value)), reverse=True):
        add(brand, brand)

    for brand in IMAGE_NAME_EXTRA_BRANDS:
        add(brand, brand)

    candidates.sort(key=lambda item: (len(item[2]), len(item[0])), reverse=True)
    return candidates


def _strip_loose_prefix(text_value: str, prefix: str) -> str:
    text_value = _normalize_product_text(text_value)
    prefix = _normalize_product_text(prefix)
    if not prefix:
        return text_value

    pattern = r"^\s*"
    for ch in prefix:
        if ch.isspace():
            pattern += r"\s*"
        elif ch in "-_·•.!":
            pattern += r"[\s\-_·•.!]*"
        else:
            pattern += re.escape(ch) + r"\s*"

    match = re.match(pattern, text_value, flags=re.IGNORECASE)
    if match:
        return text_value[match.end():].strip()
    if text_value.lower().startswith(prefix.lower()):
        return text_value[len(prefix):].strip()
    return text_value


def _clean_derived_product_name(value: Optional[str]) -> Optional[str]:
    out = _normalize_product_text(value)
    if not out:
        return None
    out = out.strip(" \n\r\t-_·•:：,，。()（）[]【】")
    out = re.sub(r"^猫粮(?=[\u4e00-\u9fffA-Za-z0-9])", "", out)
    out = out.strip(" \n\r\t-_·•:：,，。()（）[]【】")
    if not out or out in IMAGE_NAME_GENERIC_PRODUCT_NAMES:
        return None
    return out


def _looks_weak_product_name(value: Optional[str]) -> bool:
    text_value = _normalize_product_text(value)
    if not text_value:
        return True
    if text_value in IMAGE_NAME_GENERIC_PRODUCT_NAMES:
        return True
    return any(marker in text_value for marker in WEAK_PRODUCT_NAME_MARKERS)


def _looks_like_packaging_product_title(line: str) -> bool:
    text_value = _normalize_product_text(line)
    if not text_value:
        return False
    compact = re.sub(r"\s+", "", text_value)
    if _is_ingredient_section_heading(compact):
        return False
    if _looks_like_nutrition_panel(text_value):
        return False
    if _looks_like_implicit_ingredient_line(text_value):
        return False
    if any(marker in compact for marker in WEAK_PRODUCT_NAME_MARKERS):
        return False
    if any(marker in compact for marker in PRODUCT_NAME_TITLE_MARKERS):
        return 2 <= len(compact) <= 40
    return False


def _extract_product_name_from_packaging_lines(lines: Sequence[str]) -> Optional[str]:
    title_lines: List[str] = []
    brand_seen = False

    for raw in lines:
        line = _normalize(raw)
        if not line:
            continue
        compact = re.sub(r"\s+", "", line)
        lower_compact = compact.lower()
        if any(alias in lower_compact for alias in ("kuanfu", "宽福", "爱初")):
            brand_seen = True
            continue
        if not brand_seen:
            continue
        if _is_ingredient_section_heading(line) or _looks_like_implicit_ingredient_line(line):
            break
        if _looks_like_packaging_product_title(line):
            title_lines.append(line)
            continue
        if title_lines:
            break

    if not title_lines:
        for raw in lines:
            line = _normalize(raw)
            if _looks_like_packaging_product_title(line):
                title_lines.append(line)
                if len(title_lines) >= 2:
                    break

    if not title_lines:
        return None
    product_name = " ".join(title_lines[:2]).strip()
    return _clean_derived_product_name(product_name)


def _derive_brand_product_from_image_name(
    image_name: Optional[str],
    brand_candidates: Sequence[Tuple[str, str, str]],
) -> Tuple[Optional[str], Optional[str]]:
    stem = _clean_image_name_stem(image_name)
    if not stem or _looks_like_camera_filename(stem):
        return None, None

    normalized_stem = _normalize_brand_key(stem)
    for alias, canonical, alias_key in brand_candidates:
        if alias_key and normalized_stem.startswith(alias_key):
            brand = _normalize_product_text(canonical) or None
            product_name = _clean_derived_product_name(_strip_loose_prefix(stem, alias))
            return brand, product_name

    match = re.match(r"^([\u4e00-\u9fffA-Za-z]{2,8})猫粮(.+)$", stem)
    if match:
        brand = _clean_derived_product_name(match.group(1))
        product_name = _clean_derived_product_name(match.group(2))
        return brand, product_name
    return None, None


def _derive_brand_from_product_name(
    product_name: Optional[str],
    brand_candidates: Sequence[Tuple[str, str, str]],
) -> Optional[str]:
    text_value = _normalize_product_text(product_name)
    if not text_value:
        return None

    normalized_text = _normalize_brand_key(text_value)
    for _, canonical, alias_key in brand_candidates:
        if alias_key and normalized_text.startswith(alias_key):
            return _normalize_product_text(canonical) or None

    match = re.match(r"^([\u4e00-\u9fffA-Za-z0-9·!]{2,12})猫粮(?:\s|$|[：:，,、-])", text_value)
    if not match:
        return None

    brand = _clean_derived_product_name(match.group(1))
    if not brand:
        return None
    brand_key = _normalize_brand_key(brand)
    if brand_key in {_normalize_brand_key(x) for x in BRAND_PREFIX_STOP_WORDS}:
        return None
    if brand in IMAGE_NAME_GENERIC_PRODUCT_NAMES:
        return None
    return brand


def _merge_brand_product_from_image_name(
    image_name: Optional[str],
    current_brand: Optional[str],
    current_product_name: Optional[str],
    brand_candidates: Sequence[Tuple[str, str, str]],
) -> Tuple[Optional[str], Optional[str]]:
    brand = _normalize_product_text(current_brand) or None
    product_name = _normalize_product_text(current_product_name) or None
    derived_brand, derived_product_name = _derive_brand_product_from_image_name(
        image_name=image_name,
        brand_candidates=brand_candidates,
    )
    if not brand:
        brand = derived_brand
    if not brand:
        brand = _derive_brand_from_product_name(product_name, brand_candidates)
    if (not product_name) or _looks_weak_product_name(product_name):
        product_name = derived_product_name or product_name
    return brand, product_name


def _unknown_identity_suffix(source_id: Any, image_name: Optional[str]) -> str:
    source_text = _normalize_product_text(source_id)
    if source_text:
        return source_text
    stem = _clean_image_name_stem(image_name)
    stem_key = _normalize_brand_key(stem)
    return stem_key or uuid.uuid4().hex[:8]


def _apply_unknown_brand_product_defaults(
    source_id: Any,
    image_name: Optional[str],
    brand: Optional[str],
    product_name: Optional[str],
) -> Tuple[str, str]:
    normalized_brand = _normalize_product_text(brand)
    normalized_product_name = _normalize_product_text(product_name)
    if normalized_brand:
        if normalized_product_name:
            return normalized_brand, normalized_product_name
        suffix = _unknown_identity_suffix(source_id, image_name)
        return normalized_brand, f"{UNKNOWN_PRODUCT_PREFIX}_{suffix}"

    suffix = _unknown_identity_suffix(source_id, image_name)
    return f"{UNKNOWN_BRAND_PREFIX}_{suffix}", f"{UNKNOWN_PRODUCT_PREFIX}_{suffix}"


def _is_unknown_placeholder(value: Optional[str], prefix: str) -> bool:
    return _normalize_product_text(value).startswith(f"{prefix}_")


def _choose_group_brand_product(rows: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    candidates: List[Tuple[int, str, str]] = []
    for row in rows:
        brand = _normalize_product_text(row.get("brand"))
        product_name = _normalize_product_text(row.get("product_name"))
        if not brand and not product_name:
            continue
        score = 0
        if brand and not _is_unknown_placeholder(brand, UNKNOWN_BRAND_PREFIX):
            score += 100
        if product_name and not _is_unknown_placeholder(product_name, UNKNOWN_PRODUCT_PREFIX):
            score += 50
        if row.get("ingredient_composition") and _is_usable_ingredient_composition(row.get("ingredient_composition")):
            score += 10
        if brand and _normalize_brand_key(brand) in {_normalize_brand_key(x) for x in BRAND_PREFIX_STOP_WORDS}:
            score -= 80
        if product_name in IMAGE_NAME_GENERIC_PRODUCT_NAMES:
            score -= 30
        candidates.append((score, brand or "", product_name or ""))

    if not candidates:
        return None, None
    _, brand, product_name = max(candidates, key=lambda item: (item[0], len(item[1]), len(item[2])))
    return brand or None, product_name or None


def _choose_group_ingredient_composition(rows: Sequence[Dict[str, Any]]) -> Optional[str]:
    candidates = [
        str(row.get("ingredient_composition") or "")
        for row in rows
        if _is_usable_ingredient_composition(row.get("ingredient_composition"))
    ]
    if not candidates:
        return None
    return max(candidates, key=_ingredient_candidate_score)


def _row_ingredient_score(row: Dict[str, Any]) -> int:
    value = row.get("ingredient_composition")
    if not _is_usable_ingredient_composition(value):
        return -10**9
    return _ingredient_candidate_score(str(value or ""))


def _choose_group_representative(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(
        max(
            rows,
            key=lambda row: (
                _row_ingredient_score(row),
                1 if _normalize_product_text(row.get("brand")) else 0,
                1 if _normalize_product_text(row.get("product_name")) else 0,
                -int(row.get("source_id") or 0),
            ),
        )
    )


def _merge_parsed_rows_by_image_group(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for row in rows:
        group_key = _image_name_group_key(row.get("image_name")) or f"source:{row.get('source_id')}"
        if group_key not in grouped:
            grouped[group_key] = []
            order.append(group_key)
        grouped[group_key].append(dict(row))

    merged_rows: List[Dict[str, Any]] = []
    for group_key in order:
        group_rows = grouped[group_key]
        group_brand, group_product_name = _choose_group_brand_product(group_rows)
        group_ingredient = _choose_group_ingredient_composition(group_rows)
        group_source_ids = ",".join(str(row.get("source_id")) for row in group_rows if row.get("source_id") is not None)
        row = _choose_group_representative(group_rows)
        brand = group_brand or row.get("brand")
        product_name = group_product_name or row.get("product_name")
        brand, product_name = _apply_unknown_brand_product_defaults(
            source_id=row.get("source_id"),
            image_name=row.get("image_name"),
            brand=brand,
            product_name=product_name,
        )
        row["brand"] = brand
        row["product_name"] = product_name
        if group_ingredient:
            row["ingredient_composition"] = group_ingredient
        row["image_group_key"] = group_key
        row["merged_source_ids"] = group_source_ids
        merged_rows.append(row)
    return merged_rows


def _looks_like_label_line(line: str) -> bool:
    line = _normalize(line)
    if not line:
        return False
    if any(lbl in line for lbl in ALL_LABELS + INGREDIENT_TAIL_LABELS + END_SECTION_LABELS):
        return True
    return bool(re.match(r"^[\u4e00-\u9fa5A-Za-z0-9（）()·\-/]{1,14}\s*:", line))


def _is_ingredient_tail_label_line(line: str) -> bool:
    compact = re.sub(r"\s+", "", _normalize(line)).lower()
    if not compact:
        return False
    return any(compact == re.sub(r"\s+", "", label).lower() for label in INGREDIENT_TAIL_LABELS)


def _extract_single(lines: Sequence[str], labels: Sequence[str]) -> Optional[str]:
    for i, raw in enumerate(lines):
        line = _normalize(raw)
        if not line:
            continue
        lower_line = line.lower()
        for label in labels:
            label_l = label.lower()
            if label_l not in lower_line:
                continue
            m = re.search(
                rf"{re.escape(label_l)}(?:\s*[（(][^）)]*[）)])?\s*[:：]?\s*(.*)$",
                lower_line,
            )
            if m:
                val = line[m.start(1):].strip()
                val = _normalize(val)
                if val:
                    return val
            if i + 1 < len(lines):
                nxt = _normalize(lines[i + 1])
                if nxt and not _looks_like_label_line(nxt):
                    return nxt
    return None


def _extract_multiline(lines: Sequence[str], labels: Sequence[str]) -> Optional[str]:
    for i, raw in enumerate(lines):
        line = _normalize(raw)
        if not line:
            continue
        lower_line = line.lower()
        hit = None
        for label in labels:
            if label.lower() in lower_line:
                hit = label
                break
        if not hit:
            continue

        segment: List[str] = []
        m = re.search(
            rf"{re.escape(hit.lower())}(?:\s*[（(][^）)]*[）)])?\s*[:：]?\s*(.*)$",
            lower_line,
        )
        if m:
            tail = _normalize(line[m.start(1):])
            if tail:
                segment.append(tail)

        j = i + 1
        while j < len(lines):
            nxt = _normalize(lines[j])
            if not nxt:
                j += 1
                continue
            if _is_ingredient_tail_label_line(nxt):
                break
            if _looks_like_label_line(nxt) and any(x in nxt.lower() for x in [x.lower() for x in ALL_LABELS + INGREDIENT_TAIL_LABELS + END_SECTION_LABELS]):
                break
            segment.append(nxt)
            j += 1

        if segment:
            return _normalize(" ".join(segment))
    return None


def _extract_block_from_full_text(full_text: str, labels: Sequence[str]) -> Optional[str]:
    text = full_text or ""
    if not text:
        return None

    end_label_re = "|".join(re.escape(x) for x in END_SECTION_LABELS + INGREDIENT_TAIL_LABELS)
    for label in labels:
        m = re.search(
            rf"{re.escape(label)}(?:\s*[（(][^）)]*[）)])?\s*[:：]?\s*(.+?)(?=(?:\n\s*(?:{end_label_re})(?:\s*[:：])?)|$)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            cand = _normalize(m.group(1))
            if cand:
                return cand
    return None


def _is_ingredient_section_heading(line: str) -> bool:
    compact = re.sub(r"\s+", "", _normalize(line))
    if not compact:
        return False
    return any(heading.replace("、", "") in compact.replace("、", "") for heading in INGREDIENT_SECTION_HEADINGS)


def _looks_like_implicit_ingredient_line(line: str) -> bool:
    text_value = _normalize(line)
    if not text_value:
        return False
    compact = re.sub(r"\s+", "", text_value).lower()
    if compact in IMPLICIT_INGREDIENT_STOP_LINES:
        return False
    if _looks_like_label_line(text_value) and not _is_ingredient_section_heading(text_value):
        return False
    if _looks_like_nutrition_panel(text_value):
        return False
    if len(_implicit_analysis_metric_matches(text_value)) >= 1 and not any(
        kw in text_value for kw in IMPLICIT_INGREDIENT_KEYWORDS
    ):
        return False

    has_separator = any(sep in text_value for sep in ("、", "，", ",", "；", ";"))
    has_ingredient_word = any(kw in text_value for kw in IMPLICIT_INGREDIENT_KEYWORDS)
    has_percent_ingredient = bool(re.search(r"[\u4e00-\u9fffA-Za-z（）()]{1,20}\d+(?:\.\d+)?\s*%", text_value))
    return (has_separator and has_ingredient_word) or has_percent_ingredient


def _extract_implicit_ingredient_composition(lines: Sequence[str]) -> Optional[str]:
    sections: List[str] = []
    current: List[str] = []
    in_named_section = False
    suppress_until_next_ingredient_section = False

    def flush() -> None:
        nonlocal current
        if not current:
            return
        cleaned = clean_ingredient_composition_text("".join(current))
        if _is_usable_ingredient_composition(cleaned):
            sections.append(str(cleaned))
        current = []

    for raw in lines:
        line = _normalize(raw)
        if not line:
            continue

        if _is_ingredient_section_heading(line):
            flush()
            in_named_section = True
            suppress_until_next_ingredient_section = False
            continue

        compact = re.sub(r"\s+", "", line).lower()
        if compact in IMPLICIT_INGREDIENT_STOP_LINES or _is_ingredient_tail_label_line(line):
            flush()
            in_named_section = False
            suppress_until_next_ingredient_section = True
            continue

        if suppress_until_next_ingredient_section:
            continue

        if in_named_section:
            if _looks_like_label_line(line) and not _looks_like_implicit_ingredient_line(line):
                flush()
                in_named_section = False
                continue
            if _looks_like_implicit_ingredient_line(line):
                current.append(line)
            elif current:
                current.append(line)
            continue

        if _looks_like_implicit_ingredient_line(line):
            current.append(line)
            continue

        flush()

    flush()
    if not sections:
        return None
    return "、".join(sections)


def _ingredient_candidate_score(s: str) -> int:
    if not s:
        return -10**9
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", s))
    percent_cnt = s.count("%")
    sep_cnt = s.count("、") + s.count("，") + s.count(",")
    penalty = 0
    low = s.lower()
    for kw in ["authentic", "wholeprey", "diet", "nutrition", "ancestor", "delicious"]:
        if kw in low:
            penalty += 50
    return chinese_chars * 2 + percent_cnt * 3 + sep_cnt - penalty


def _normalize_ingredient_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    out = str(s)
    # 中文配料表里换行断词较常见，去掉所有空白更稳妥
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", out))
    if chinese_chars >= 10:
        out = re.sub(r"\s+", "", out)
    else:
        out = re.sub(r"\s+", " ", out).strip()
    out = out.strip(" ;,，。\"'{}[]")
    return out or None


def _strip_ingredient_head_noise(s: Optional[str]) -> Optional[str]:
    if not s:
        return None

    txt = str(s).strip()
    if not txt:
        return None

    low = txt.lower()
    matches: List[tuple[int, int, str]] = []
    for label in INGREDIENT_HEAD_LABELS:
        label_low = label.lower()
        start = 0
        while True:
            pos = low.find(label_low, start)
            if pos < 0:
                break
            end = pos + len(label)
            tail = low[end:end + 16].lstrip()
            if label in {"成分", "ingredient", "ingredients"} and tail.startswith(("分析", "保证", "营养")):
                start = pos + 1
                continue
            matches.append((pos, end, label))
            start = pos + 1

    if matches:
        early_matches = [item for item in matches if item[0] <= 160]
        pos, end, _ = max(early_matches or matches, key=lambda item: (item[1], len(item[2])))
        if end < len(txt):
            txt = txt[end:].strip()

    txt = re.sub(r"^(?:ingredient\s*list|ingredients\s*list|list)(?=[\u4e00-\u9fffA-Za-z0-9（(])", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"^(?:原料|配料|成分)?表(?=[\u4e00-\u9fffA-Za-z0-9（(])", "", txt)
    txt = txt.strip("：:;；,，|｜·• ")
    return txt or None


def _looks_like_nutrition_panel(s: Optional[str]) -> bool:
    if not s:
        return False

    compact = re.sub(r"\s+", "", str(s)).lower()
    if not compact:
        return False

    head = compact[:180]
    if any(pattern in head for pattern in ANALYSIS_START_PATTERNS):
        return True
    if ("kcal" in head or "卡路里" in head) and ("营养素" in head or "營養素" in head):
        return True
    metric_hits = sum(1 for kw in ANALYSIS_METRIC_KEYWORDS if kw.lower() in head)
    bound_hits = head.count("%") + head.count("≥") + head.count("≤")
    if metric_hits >= 4 and bound_hits >= 2:
        return True
    return False


def _looks_suspicious_ingredient_text(s: Optional[str]) -> bool:
    if not s:
        return False

    txt = str(s).strip()
    if not txt:
        return False

    compact = re.sub(r"\s+", "", txt).lower()
    if compact.startswith(("表", "分析", "营养成分表", "營養成分表", "产品成分分析")):
        return True
    if any(marker.lower() in compact for marker in SUSPICIOUS_INGREDIENT_MARKERS):
        return True
    if re.search(r"[A-Z]{8,}", txt) and any(lbl in txt for lbl in ["原料组成", "配料表", "原料表", "成分表"]):
        return True
    return False


def clean_ingredient_composition_text(s: Optional[str]) -> Optional[str]:
    if not s:
        return None

    out = str(s).strip()
    if not out:
        return None

    out = _strip_ingredient_head_noise(out) or ""
    out = _trim_ingredient_tail(out) or ""
    out = _normalize_ingredient_text(out) or ""
    if not out:
        return None
    if _looks_like_nutrition_panel(out):
        return None

    out = out.replace("％", "%")
    out = out.replace("“", "").replace("”", "").replace("\"", "").replace("'", "")
    out = out.replace("《", "(").replace("》", ")").replace("「", "(").replace("」", ")")
    out = out.replace("【", "(").replace("】", ")").replace("〔", "(").replace("〕", ")")
    out = re.sub(r"(?<=[A-Za-z0-9Α-Ωα-ωΒβ])[,，](?=[A-Za-z0-9Α-Ωα-ωΒβ-])", "__INNER_COMMA__", out)
    out = out.replace("•", "、").replace("·", "、").replace("・", "、").replace("●", "、")
    out = out.replace("|", "、").replace("｜", "、").replace("¦", "、")
    out = out.replace("。", "、").replace("，", "、").replace(",", "、")
    out = out.replace("；", "、").replace(";", "、").replace("：", "、").replace(":", "、")
    out = out.replace("\u3000", "")
    out = re.sub(r"\s+", "", out)

    # Protect decimal points inside percentages such as 6.5%.
    out = re.sub(r"(?<=\d)\.(?=\d)", "__DECIMALTOKEN__", out)

    # OCR often uses ASCII dots / hyphens as separators outside numeric decimals.
    out = out.replace(".", "、")
    out = re.sub(r"(?<=[\u4e00-\u9fff%）)])[-_](?=[\u4e00-\u9fffA-Za-z])", "、", out)

    # When OCR drops punctuation, a closing percentage or bracket immediately followed by text
    # is usually a boundary between two ingredients.
    out = re.sub(r"(?<=%)(?=[A-Za-z\u4e00-\u9fff])", "、", out)
    out = re.sub(r"(?<=[）)])(?=(?!合物)[A-Za-z\u4e00-\u9fff])", "、", out)

    out = out.replace("__DECIMALTOKEN__", ".")
    out = out.replace("__INNER_COMMA__", ",")
    out = out.replace("脱、水", "脱水")
    out = re.sub(r"、{2,}", "、", out)

    parts = []
    for token in out.split("、"):
        token = token.strip("、,，。;；:： ")
        if not token:
            continue
        parts.append(token)

    return "、".join(parts) if parts else None


def _implicit_analysis_metric_matches(txt: Optional[str]) -> List[re.Match[str]]:
    if not txt:
        return []
    metric_re = re.compile(
        rf"(?P<metric>{'|'.join(IMPLICIT_ANALYSIS_METRIC_PATTERNS)})"
        rf"(?:[\s:：,，()（）\-/]|以|计){{0,16}}"
        rf"(?P<value>{IMPLICIT_ANALYSIS_VALUE_PATTERN})",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return list(metric_re.finditer(str(txt)))


def _is_usable_ingredient_composition(s: Optional[str]) -> bool:
    if not s:
        return False

    txt = str(s).strip()
    if not txt or len(re.sub(r"\s+", "", txt)) < 8:
        return False
    if _looks_suspicious_ingredient_text(txt):
        return False
    if _looks_like_nutrition_panel(txt):
        return False
    compact = re.sub(r"\s+", "", txt).lower()
    if len(_implicit_analysis_metric_matches(txt)) >= 2:
        return False

    token_cnt = len([token for token in re.split(r"[、,，;；]", txt) if token.strip()])
    if token_cnt >= 2:
        return True
    if "%" in txt and token_cnt >= 1 and len(compact) >= 20:
        return True
    return False


def _trim_ingredient_tail(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    txt = str(s)
    low = txt.lower()
    cut_idx: Optional[int] = None
    for label in INGREDIENT_TAIL_LABELS:
        pos = low.find(label.lower())
        if pos <= 0:
            continue
        if cut_idx is None or pos < cut_idx:
            cut_idx = pos
    if cut_idx is not None:
        txt = txt[:cut_idx]
    metric_matches = _implicit_analysis_metric_matches(txt)
    if len(metric_matches) >= 2:
        first_metric_pos = metric_matches[0].start()
        if first_metric_pos > 0:
            txt = txt[:first_metric_pos]
    txt = txt.rstrip("。.;；:：,， \n\r\t")
    return txt or None


def _extract_phone(text: str, lines: Sequence[str]) -> Optional[str]:
    from_label = _extract_single(lines, PHONE_LABELS)
    if from_label:
        m = re.search(r"((?:\+?86[-\s]?)?(?:400[-\s]?\d{3}[-\s]?\d{4}|1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8}))", from_label)
        if m:
            return _normalize(m.group(1))
        return _normalize(from_label)
    m = re.search(r"((?:\+?86[-\s]?)?(?:400[-\s]?\d{3}[-\s]?\d{4}|1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8}))", text)
    return _normalize(m.group(1)) if m else None


def _ocr_json_to_text_and_lines(ocr_json_text: str) -> tuple[str, List[str], Any]:
    obj: Any
    try:
        obj = json.loads(ocr_json_text or "{}")
    except Exception:
        obj = {"full_text": str(ocr_json_text or "")}

    full_text = ""
    lines: List[str] = []
    if isinstance(obj, dict):
        full_text = str(obj.get("full_text") or "").strip()
        raw_lines = obj.get("lines")
        if isinstance(raw_lines, list):
            lines = [_normalize(str(x)) for x in raw_lines if _normalize(str(x))]
        if not full_text:
            parsed_results = obj.get("ParsedResults")
            if isinstance(parsed_results, list):
                chunks = []
                for item in parsed_results:
                    if isinstance(item, dict):
                        t = str(item.get("ParsedText") or "").strip()
                        if t:
                            chunks.append(t)
                full_text = "\n".join(chunks).strip()
    elif isinstance(obj, list):
        full_text = "\n".join(str(x) for x in obj if x).strip()
    else:
        full_text = str(obj).strip()

    if not lines:
        lines = [_normalize(x) for x in re.split(r"[\r\n]+", full_text) if _normalize(x)]

    return full_text, lines, obj


def parse_ocr_json_fields(ocr_json_text: str) -> Dict[str, Optional[str]]:
    full_text, lines, _ = _ocr_json_to_text_and_lines(ocr_json_text)
    product_name = _extract_single(lines, NAME_LABELS)
    if _looks_weak_product_name(product_name):
        product_name = _extract_product_name_from_packaging_lines(lines) or product_name

    ingredient_candidates: List[str] = []
    c1 = _extract_block_from_full_text(full_text, INGREDIENT_LABELS)
    if c1:
        ingredient_candidates.append(c1)
    c2 = _extract_multiline(lines, INGREDIENT_LABELS)
    if c2:
        ingredient_candidates.append(c2)
    # 英文标签只做兜底，避免优先命中英文营销文案
    c3 = _extract_block_from_full_text(full_text, INGREDIENT_LABELS_FALLBACK)
    if c3:
        ingredient_candidates.append(c3)
    c4 = _extract_multiline(lines, INGREDIENT_LABELS_FALLBACK)
    if c4:
        ingredient_candidates.append(c4)
    c5 = _extract_implicit_ingredient_composition(lines)
    if c5:
        ingredient_candidates.append(c5)

    ingredient_composition = None
    cleaned_candidates: List[str] = []
    for candidate in ingredient_candidates:
        cleaned_candidate = _trim_ingredient_tail(candidate)
        cleaned_candidate = _normalize_ingredient_text(cleaned_candidate)
        cleaned_candidate = clean_ingredient_composition_text(cleaned_candidate)
        if _is_usable_ingredient_composition(cleaned_candidate):
            cleaned_candidates.append(str(cleaned_candidate))
    if cleaned_candidates:
        ingredient_composition = max(
            cleaned_candidates,
            key=_ingredient_candidate_score,
        )

    phone = _extract_phone(full_text, lines)
    importer = _extract_single(lines, IMPORTER_LABELS)
    return {
        "product_name": _normalize(product_name or "") or None,
        "ingredient_composition": ingredient_composition,
        "phone": _normalize(phone or "") or None,
        "importer": _normalize(importer or "") or None,
        "ocr_text": full_text or None,
    }


def clean_catfood_ingredient_compositions(
    engine: Engine,
    table_name: str = "catfood_ingredient_ocr_parsed",
    limit: Optional[int] = None,
    create_backup: bool = True,
) -> IngredientCleanSummary:
    table_name = _safe_table(table_name)
    lim = int(limit or 0)
    batch_id = uuid.uuid4().hex[:12]

    fetch_sql = f"""
    SELECT id, source_id, image_name, brand, product_name, ingredient_composition, ocr_json
    FROM `{table_name}`
    WHERE (
          ingredient_composition IS NOT NULL
      AND TRIM(ingredient_composition) <> ''
    ) OR (
          ingredient_composition IS NULL
      AND ocr_json IS NOT NULL
      AND TRIM(ocr_json) <> ''
    )
    ORDER BY id ASC
    """
    if lim > 0:
        fetch_sql += "\nLIMIT :lim"

    with engine.begin() as conn:
        params = {"lim": lim} if lim > 0 else {}
        rows = conn.execute(text(fetch_sql), params).mappings().all()

    updates: List[Dict[str, Any]] = []
    brand_candidates = _build_image_name_brand_candidates(_load_known_brands(engine, table_name))
    brand_updated = 0
    product_name_updated = 0
    for row in rows:
        raw_db_value = row.get("ingredient_composition")
        raw_value = str(raw_db_value or "")
        cleaned_value = clean_ingredient_composition_text(raw_value) if raw_value else None
        if cleaned_value is not None and not _is_usable_ingredient_composition(cleaned_value):
            cleaned_value = None
        if (not raw_value) or _looks_suspicious_ingredient_text(raw_value) or cleaned_value is None:
            reparsed_value: Optional[str] = None
            ocr_json_text = str(row.get("ocr_json") or "")
            if ocr_json_text:
                reparsed_value = parse_ocr_json_fields(ocr_json_text).get("ingredient_composition")
            if _is_usable_ingredient_composition(reparsed_value):
                cleaned_value = reparsed_value
        current_brand = _normalize_product_text(row.get("brand")) or None
        current_product_name = _normalize_product_text(row.get("product_name")) or None
        cleaned_brand, cleaned_product_name = _merge_brand_product_from_image_name(
            image_name=row.get("image_name"),
            current_brand=current_brand,
            current_product_name=current_product_name,
            brand_candidates=brand_candidates,
        )
        cleaned_brand, cleaned_product_name = _apply_unknown_brand_product_defaults(
            source_id=row.get("source_id"),
            image_name=row.get("image_name"),
            brand=cleaned_brand,
            product_name=cleaned_product_name,
        )
        normalized_current = raw_value or None
        brand_changed = cleaned_brand != current_brand
        product_name_changed = cleaned_product_name != current_product_name
        if cleaned_value != normalized_current or brand_changed or product_name_changed:
            if brand_changed:
                brand_updated += 1
            if product_name_changed:
                product_name_updated += 1
            updates.append(
                {
                    "id": int(row["id"]),
                    "brand": cleaned_brand,
                    "product_name": cleaned_product_name,
                    "ingredient_composition": cleaned_value,
                }
            )

    backup_table: Optional[str] = None
    if updates:
        with engine.begin() as conn:
            if create_backup:
                backup_table = f"{table_name}_bak_{batch_id}"
                conn.execute(text(f"CREATE TABLE `{backup_table}` LIKE `{table_name}`"))
                conn.execute(text(f"INSERT INTO `{backup_table}` SELECT * FROM `{table_name}`"))
            conn.execute(
                text(
                    f"""
                    UPDATE `{table_name}`
                    SET brand = :brand,
                        product_name = :product_name,
                        ingredient_composition = :ingredient_composition
                    WHERE id = :id
                    """
                ),
                updates,
            )

    return IngredientCleanSummary(
        scanned=len(rows),
        updated=len(updates),
        brand_updated=brand_updated,
        product_name_updated=product_name_updated,
        table_name=table_name,
        batch_id=batch_id,
        backup_table=backup_table,
    )


def parse_catfood_ingredient_ocr_json(
    engine: Engine,
    source_table: str = "catfood_ingredient_ocr_results",
    target_table: str = "catfood_ingredient_ocr_parsed",
    limit: int = 500,
    incremental_only: bool = True,
) -> ParseSummary:
    source_table = _safe_table(source_table)
    target_table = _safe_table(target_table)
    ensure_parsed_table(engine, target_table)

    lim = max(1, int(limit))
    if incremental_only:
        fetch_sql = f"""
        SELECT s.id, s.image_path, s.image_name, s.file_sha256, s.ocr_json
        FROM `{source_table}` s
        LEFT JOIN `{target_table}` t
          ON t.source_id = s.id
        LEFT JOIN `{target_table}` gm
          ON gm.source_id <> s.id
         AND gm.merged_source_ids IS NOT NULL
         AND FIND_IN_SET(CAST(s.id AS CHAR), gm.merged_source_ids) > 0
        WHERE gm.id IS NULL
          AND (
              t.source_id IS NULL
           OR t.ingredient_composition IS NULL
           OR TRIM(t.ingredient_composition) = ''
           OR t.brand IS NULL
           OR TRIM(t.brand) = ''
           OR t.product_name IS NULL
           OR TRIM(t.product_name) = ''
          )
        ORDER BY s.id ASC
        LIMIT :lim
        """
    else:
        fetch_sql = f"""
        SELECT s.id, s.image_path, s.image_name, s.file_sha256, s.ocr_json
        FROM `{source_table}` s
        ORDER BY s.id DESC
        LIMIT :lim
        """
    with engine.begin() as conn:
        rows = conn.execute(text(fetch_sql), {"lim": lim}).mappings().all()

    batch_id = uuid.uuid4().hex[:12]
    if not rows:
        return ParseSummary(scanned=0, upserted=0, source_table=source_table, target_table=target_table, batch_id=batch_id)

    brand_candidates = _build_image_name_brand_candidates(_load_known_brands(engine, target_table))
    parsed_payload = []
    for r in rows:
        parsed = parse_ocr_json_fields(str(r.get("ocr_json") or ""))
        brand, product_name = _merge_brand_product_from_image_name(
            image_name=r.get("image_name"),
            current_brand=None,
            current_product_name=parsed.get("product_name"),
            brand_candidates=brand_candidates,
        )
        parsed_payload.append(
            {
                "source_id": int(r["id"]),
                "image_path": str(r.get("image_path") or "") or None,
                "image_name": str(r.get("image_name") or "") or None,
                "file_sha256": str(r.get("file_sha256") or "") or None,
                "image_group_key": None,
                "merged_source_ids": None,
                "brand": brand,
                "product_name": product_name,
                "ingredient_composition": parsed.get("ingredient_composition"),
                "phone": parsed.get("phone"),
                "importer": parsed.get("importer"),
                "ocr_text": parsed.get("ocr_text"),
                "ocr_json": str(r.get("ocr_json") or "") or None,
                "parse_batch_id": batch_id,
            }
        )
    payload = _merge_parsed_rows_by_image_group(parsed_payload)

    upsert_sql = f"""
    INSERT INTO `{target_table}`(
      source_id, image_path, image_name, file_sha256, image_group_key, merged_source_ids,
      brand, product_name, ingredient_composition,
      phone, importer, ocr_text, ocr_json, parse_batch_id, parse_ts
    )
    VALUES(
      :source_id, :image_path, :image_name, :file_sha256, :image_group_key, :merged_source_ids,
      :brand, :product_name, :ingredient_composition,
      :phone, :importer, :ocr_text, :ocr_json, :parse_batch_id, NOW()
    )
    ON DUPLICATE KEY UPDATE
      image_path=VALUES(image_path),
      image_name=VALUES(image_name),
      file_sha256=VALUES(file_sha256),
      image_group_key=VALUES(image_group_key),
      merged_source_ids=VALUES(merged_source_ids),
      brand=VALUES(brand),
      product_name=VALUES(product_name),
      ingredient_composition=VALUES(ingredient_composition),
      phone=VALUES(phone),
      importer=VALUES(importer),
      ocr_text=VALUES(ocr_text),
      ocr_json=VALUES(ocr_json),
      parse_batch_id=VALUES(parse_batch_id),
      parse_ts=NOW()
    """
    with engine.begin() as conn:
        conn.execute(text(upsert_sql), payload)

    return ParseSummary(
        scanned=len(rows),
        upserted=len(payload),
        source_table=source_table,
        target_table=target_table,
        batch_id=batch_id,
    )
