#!/usr/bin/env python3
"""Build reviewable standard-product candidates from cat-food OCR text.

The OCR source of truth is ``catfood_ingredient_ocr_results.ocr_text``.
``catfood_ingredient_ocr_parsed`` is only used to locate the OCR row and provide
the current brand candidate. The script deliberately writes to a candidate
table instead of the production product master.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pymysql
import yaml
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config, get_qwen_config  # noqa: E402


DEFAULT_SOURCE_TABLE = "catfood_ingredient_ocr_results"
DEFAULT_PARSED_TABLE = "catfood_ingredient_ocr_parsed"
DEFAULT_TARGET_TABLE = "catfood_standard_product_candidate"
BRAND_MASTER_PATH = (
    BASE_DIR / "vendor" / "csv_mysql_labeling" / "config" / "catfood_brand_master.yaml"
)

QUALITY_RANK = {"weak": 1, "medium": 2, "strong": 3}
PROMPT_VERSION = "catfood-product-display-v2"

CANDIDATE_TYPES = {
    "model_code",
    "series_name",
    "official_name",
    "flavor_protein",
    "process_type",
    "function_position",
    "life_stage",
    "feeding_instruction",
    "ingredient_text",
    "ocr_noise",
    "unknown",
}
NAME_QUALITIES = {"strong", "medium", "weak", "invalid"}
REVIEW_STATUSES = {"pending", "rejected", "needs_manual_review"}
TYPE_ALIASES = {
    "model": "model_code",
    "sku_code": "model_code",
    "series": "series_name",
    "official_product_name": "official_name",
    "product_name": "official_name",
    "flavor": "flavor_protein",
    "protein": "flavor_protein",
    "function": "function_position",
    "process": "process_type",
    "ingredient": "ingredient_text",
    "noise": "ocr_noise",
}
QUALITY_ALIASES = {
    "high": "strong",
    "good": "strong",
    "moderate": "medium",
    "low": "weak",
    "reject": "invalid",
}
REVIEW_STATUS_ALIASES = {
    "needs_review": "needs_manual_review",
    "manual_review": "needs_manual_review",
    "review": "needs_manual_review",
    "invalid": "rejected",
}

GENERIC_WEAK_TERMS = {
    "低敏",
    "美毛",
    "免疫",
    "益生菌",
    "室内",
    "毛球管理",
    "冻干猫粮",
    "烘焙猫粮",
    "风干猫粮",
    "湿粮",
}

LIFE_STAGE_DISPLAY_MAP = {
    "幼猫": "幼猫粮",
    "幼猫粮": "幼猫粮",
    "幼年期": "幼猫粮",
    "成猫": "成猫粮",
    "成猫粮": "成猫粮",
    "成年期": "成猫粮",
    "全龄": "全阶段猫粮",
    "全龄猫粮": "全阶段猫粮",
    "全年龄": "全阶段猫粮",
    "全阶段": "全阶段猫粮",
    "全阶段猫粮": "全阶段猫粮",
    "全期": "全阶段猫粮",
}

HARD_REJECT_EXACT = {
    "配方成分分析",
    "配方主要成分",
    "成分分析",
    "产品成分分析保证值",
    "营养分析保证值",
}

ADDITIVE_LONG_RE = re.compile(
    r"(?:维生素|生育酚|硝酸硫胺|核黄素|吡哆醇|氰钴胺|烟酰胺|"
    r"泛酸钙|生物素|叶酸|氯化胆碱|硫酸(?:锌|铜|锰|亚铁)|"
    r"蛋氨酸|牛磺酸|磷酸氢钙|添加剂组成)"
)
FEEDING_LONG_RE = re.compile(
    r"(?:每日建议|推荐饲喂|喂食指南|喂食方法|饲喂量|换粮步骤|"
    r"请确保|开袋即食|储存|贮存|适用(?:年龄|对象|猫种|阶段))"
)
IDENTIFIER_NOISE_RE = re.compile(
    r"(?:小红书号|条形码|条码|批号|批次|生产日期|有效期|"
    r"\bLOT\b|\bEXP\b|\bBB\b|\bDD/MM/YYYY\b)",
    re.I,
)
COORDINATE_OR_NUMERIC_RE = re.compile(
    r"^(?:[\d\s,，.:：;/|%+\-_=]+|(?:\d+[,，]){2,}\d+)$"
)
OCR_GARBAGE_RE = re.compile(r"^[^A-Za-z\u4e00-\u9fff]{2,}$")

SYSTEM_PROMPT = """你是宠物食品 OCR 产品名称清洗助手。

任务：
判断输入文本是否可以作为猫粮产品展示名，并生成结构化结果。

判断原则：
1. 不是所有 OCR 文本都是产品名。必须先判断文本类型。
2. product_name_display 是前端主展示名，不要包含品牌名。
3. 如果文本是营养添加剂、配方成分、喂养说明、适用年龄、适用猫种、批号、坐标、渠道词、营销词或 OCR 垃圾，判定为 invalid。
4. 如果文本包含明确型号编码，如 K36、BK34、N5、R9、kd、md，优先作为 display。
5. 如果文本包含明确系列名，如 八重守护、牧场盛宴、南瓜系列、Indigo Moon系列，作为 display。
6. 如果文本是正式商品名，如 原始猎食原味、天然猫粮2.0，可以作为 display。
7. 如果文本是主蛋白/口味，如 三文鱼成猫粮、鸡肉猫粮、牛肉猫粮，可以作为 medium display。
8. 如果文本只是低敏、美毛、免疫、益生菌、室内、毛球管理、冻干猫粮、烘焙猫粮等泛功能/工艺词，判定为 weak，优先放入 subtitle 或 normalized_tags，不要直接确认为标准产品名。
9. 幼猫、成猫、全龄、全阶段属于可进入主展示名的适用阶段候选词。分别规范为“幼猫粮”“成猫粮”“全阶段猫粮”，candidate_type=life_stage，name_quality=medium。
10. 如果文本疑似被截断，如“鲜肉全价成”“野性本能中大型全”，判定为 pending，并给出 truncation_suspected=true。
11. 只输出 JSON，不要输出解释性段落。

输出字段：
{
  "is_product_name_candidate": true,
  "product_name_display": null,
  "product_name_subtitle": null,
  "candidate_type": "unknown",
  "name_quality": "invalid",
  "review_status": "rejected",
  "normalized_tags": [],
  "truncation_suspected": false,
  "reject_reason": null,
  "reason": ""
}"""

NOISE_TERMS = {
    "宝贝",
    "评价",
    "详情",
    "推荐",
    "客服",
    "店铺",
    "顶部",
    "直播讲解",
    "加入购物车",
    "领券购买",
    "产品信息",
    "product information",
    "产品名称",
    "product name",
}

GENERIC_PRODUCT_NAMES = {
    "猫粮",
    "全价猫粮",
    "宠物全价猫粮",
    "全价宠物食品猫粮",
    "全价宠物食品成年期猫粮",
    "全价宠物食品幼年期猫粮",
    "成猫粮",
    "幼猫粮",
}

PRODUCT_LABEL_RE = re.compile(
    r"(?:产品名称|商品名称|通用名称|品名|PRODUCT\s*NAME)"
    r"\s*[:：]?\s*(?:PRODUCT\s*NAME\s*)?"
    r"([^\n|]{2,100})",
    re.IGNORECASE,
)

LABEL_TAIL_RE = re.compile(
    r"\s+(?:产品规格|包装规格|规格|净含量|适用(?:对象|阶段|猫种|年龄)|"
    r"保质期|原料组成|配料组成|成分表|产地|储存方式|贮存条件)\b.*$",
    re.IGNORECASE,
)

MODEL_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,5}[- ]?\d{1,4}(?:[./-]\d{1,3})?)(?![A-Za-z0-9])"),
)

MODEL_STOPWORDS = {
    "omega3",
    "omega6",
    "dha",
    "epa",
    "b1",
    "b2",
    "b3",
    "b5",
    "b6",
    "b7",
    "b9",
    "b12",
    "d3",
    "cfu",
    "kg",
    "mg",
    "iu",
    "ph",
    "vip",
}

MODEL_UNIT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:KG|G|MG|IU|ML|LBS?|LB|MM|CM|CFU|KCAL)$",
    re.I,
)

MODEL_NOISE_RE = re.compile(
    r"(?:OMEGA|DHA|EPA|VIP|CFU|KCAL|VITAMIN|维生素|日期|批次)", re.I
)

SERIES_SUFFIXES = ("系列", "series")
SERIES_PATTERNS = (
    re.compile(r"(?:产品系列|所属系列)\s*[:：]?\s*([A-Za-z0-9\u4e00-\u9fff·&+\- ]{2,40}?系列)", re.I),
    re.compile(r"([A-Za-z0-9\u4e00-\u9fff·&+\-]{2,30}(?:系列|Series))", re.I),
)

FUNCTION_TERMS = (
    "肠胃呵护",
    "肠胃护理",
    "泌尿呵护",
    "泌尿护理",
    "毛球管理",
    "化毛",
    "美毛",
    "护肤美毛",
    "体重管理",
    "控重",
    "减肥",
    "低敏",
    "敏感",
    "免疫",
    "绝育",
    "室内",
    "老年呵护",
    "幼猫成长",
    "母猫孕育",
    "易消化",
    "益生菌",
)

PROCESS_TERMS = (
    "微蒸风干",
    "蒸鲜",
    "双重风干",
    "低温风干",
    "风干",
    "低温烘焙",
    "鲜肉烘焙",
    "烘焙",
    "冻干拼配",
    "冻干",
    "膨化",
    "湿粮",
)

PROTEIN_ALIASES = (
    ("马鲛鱼羊肉", ("马鲛鱼", "羊肉")),
    ("鸡肉鱼肉", ("鸡鱼", "鸡肉鱼肉")),
    ("三文鱼", ("三文鱼", "鲑鱼")),
    ("海洋鱼", ("海洋鱼", "鳕鱼", "白鱼")),
    ("鸡肉", ("鲜鸡肉", "鸡肉", "鸡味")),
    ("鸭肉", ("鲜鸭肉", "鸭肉", "鸭味")),
    ("兔肉", ("鲜兔肉", "兔肉", "兔味")),
    ("牛肉", ("鲜牛肉", "冻牛肉", "牛肉", "牛味")),
    ("羊肉", ("鲜羊肉", "羊肉", "羊味")),
    ("鹿肉", ("鹿肉", "鹿味")),
    ("乳鸽", ("乳鸽", "鸽肉")),
    ("金枪鱼", ("金枪鱼",)),
)

LIFE_STAGE_PATTERNS = (
    ("幼猫", re.compile(r"幼猫|幼年期|kitten", re.I)),
    ("成猫", re.compile(r"成猫|成年期|adult cat", re.I)),
    ("老年猫", re.compile(r"老年猫|老龄猫|senior cat", re.I)),
    ("全阶段", re.compile(r"全阶段|全龄|全年龄|全期|all life stages", re.I)),
)

PRODUCT_TYPE_PATTERNS = (
    ("湿粮", re.compile(r"湿粮|主食罐|餐包|wet food", re.I)),
    ("冻干", re.compile(r"冻干猫粮|全价冻干|主食冻干", re.I)),
    ("干粮", re.compile(r"干粮|猫粮", re.I)),
)


@dataclass
class ProductCandidate:
    brand_id: int | None
    standard_brand_name: str
    standard_product_name: str
    display_name: str
    display_name_rule: str
    display_subtitle: str = ""
    candidate_type: str = "unknown"
    review_status: str = "pending"
    normalized_tags: list[str] = field(default_factory=list)
    truncation_suspected: bool = False
    reject_reason: str = ""
    model_reason: str = ""
    hard_filter_reason: str = ""
    model_name_used: str = ""
    model_prompt_version: str = ""
    model_raw_result: dict[str, Any] = field(default_factory=dict)
    model_name: str = ""
    series_name: str = ""
    function_name: str = ""
    process_name: str = ""
    flavor_or_protein: str = ""
    product_type: str = ""
    life_stage: str = ""
    quality_level: str = "weak"
    quality_reasons: list[str] = field(default_factory=list)
    source_ids: list[int] = field(default_factory=list)
    parsed_row_ids: list[int] = field(default_factory=list)
    raw_product_names: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _safe_table(name: str) -> str:
    value = str(name or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"invalid table name: {name}")
    return value


def _clean_space(value: Any) -> str:
    text = str(value or "").replace("\u3000", " ").replace("™", "")
    text = re.sub(r"[\t\r]+", " ", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    return text.strip()


def _normalize_key(value: Any) -> str:
    text = _clean_space(value).lower()
    return re.sub(r"[\s·•._\-—–/\\|,:：;；，。()（）\[\]【】'\"®™]+", "", text)


def hard_filter_candidate(raw_text: Any) -> tuple[bool, str | None]:
    """Reject only deterministic garbage before spending a model call."""
    text = _clean_space(raw_text).strip(" -—–:：,，;；()（）[]【】")
    normalized = _normalize_key(text)
    if not text:
        return False, "empty"
    if COORDINATE_OR_NUMERIC_RE.fullmatch(text) or OCR_GARBAGE_RE.fullmatch(text):
        return False, "numeric_coordinate_or_garbled"
    if len(normalized) <= 1:
        return False, "too_short"
    if _normalize_key(text) in {_normalize_key(item) for item in HARD_REJECT_EXACT}:
        return False, "formula_analysis_heading"
    if IDENTIFIER_NOISE_RE.search(text):
        return False, "identifier_or_batch_text"
    if len(text) >= 22 and ADDITIVE_LONG_RE.search(text):
        return False, "nutrition_or_additive_sentence"
    if len(text) >= 22 and FEEDING_LONG_RE.search(text):
        return False, "feeding_instruction_sentence"
    if re.search(r"(?:宝贝|评价|详情|推荐|客服|店铺|加入购物车|领券购买|直播)", text):
        return False, "channel_or_marketing_text"
    return True, None


def build_context_text(ocr_text: str, raw_text: str, *, max_chars: int = 800) -> str:
    text = _clean_space(ocr_text)
    if not text:
        return ""
    index = text.lower().find(_clean_space(raw_text).lower())
    if index < 0:
        return text[:max_chars]
    half = max_chars // 2
    return text[max(0, index - half): index + len(raw_text) + half][:max_chars]


def brand_aliases_for(
    brand_id: int | None,
    brand_tokens: list[tuple[str, dict[str, Any]]],
) -> list[str]:
    if brand_id is None:
        return []
    aliases = {
        _clean_space(token)
        for token, row in brand_tokens
        if row.get("brand_id") == brand_id and _clean_space(token)
    }
    return sorted(aliases, key=len, reverse=True)


def _extract_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("model response does not contain a JSON object")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON is not an object")
    return parsed


def strip_brand_from_display(
    value: Any,
    brand_name: str,
    aliases: Iterable[str],
) -> str:
    text = _clean_space(value)
    tokens = sorted(
        {token for token in [brand_name, *aliases] if _clean_space(token)},
        key=len,
        reverse=True,
    )
    for token in tokens:
        text = re.sub(re.escape(_clean_space(token)), "", text, flags=re.I)
    return text.strip(" -—–:：,，;；()（）")


def _looks_truncated(value: str) -> bool:
    text = _clean_space(value)
    if not text:
        return False
    if re.search(r"(?:全价成|中大型全|鲜肉全价成|全价宠物食|成年期猫|幼年期猫)$", text):
        return True
    return text.endswith(("、", "，", ",", "的", "及", "和", "与", "&", "-", "—"))


def normalize_life_stage_display(value: Any) -> str:
    text = _clean_space(value)
    key = _normalize_key(text)
    for alias, display in LIFE_STAGE_DISPLAY_MAP.items():
        if key == _normalize_key(alias):
            return display
    if re.fullmatch(r"(?:全价)?(?:幼猫|幼年期|幼年猫)(?:猫粮)?", text):
        return "幼猫粮"
    if re.fullmatch(r"(?:全价)?(?:成猫|成年期)(?:猫粮)?", text):
        return "成猫粮"
    if re.fullmatch(r"(?:全价)?(?:全龄|全年龄|全阶段|全期)(?:猫粮)?", text):
        return "全阶段猫粮"
    return ""


def infer_candidate_type(display: str, raw_text: str) -> str:
    value = _clean_space(display or raw_text)
    if not value:
        return "unknown"
    if re.fullmatch(r"[A-Za-z]{1,5}[- ]?\d{1,4}(?:[./-]\d{1,3})?", value):
        return "model_code"
    if re.search(r"系列|Series", value, re.I):
        return "series_name"
    if normalize_life_stage_display(value):
        return "life_stage"
    if _normalize_key(value) in {_normalize_key(item) for item in GENERIC_WEAK_TERMS}:
        if any(term in value for term in PROCESS_TERMS):
            return "process_type"
        return "function_position"
    if re.search(r"三文鱼|鸡肉|鸭肉|兔肉|牛肉|羊肉|鹿肉|乳鸽|鱼肉", value):
        if re.search(r"猫粮|配方|口味", value):
            return "flavor_protein"
    if 2 <= len(_normalize_key(value)) <= 40:
        return "official_name"
    return "unknown"


def validate_model_result(
    result: dict[str, Any],
    *,
    raw_text: str,
    brand_name: str,
    aliases: Iterable[str],
) -> dict[str, Any]:
    """Normalize enums and prevent unsafe model output from becoming display data."""
    output = dict(result or {})
    candidate_type = str(output.get("candidate_type") or "unknown").strip()
    candidate_type = TYPE_ALIASES.get(candidate_type, candidate_type)
    if candidate_type not in CANDIDATE_TYPES:
        candidate_type = "unknown"
    quality = str(output.get("name_quality") or "invalid").strip()
    quality = QUALITY_ALIASES.get(quality, quality)
    if quality not in NAME_QUALITIES:
        quality = "invalid"
    review_status = str(output.get("review_status") or "needs_manual_review").strip()
    review_status = REVIEW_STATUS_ALIASES.get(review_status, review_status)
    if review_status not in REVIEW_STATUSES:
        review_status = "needs_manual_review"

    display = strip_brand_from_display(output.get("product_name_display"), brand_name, aliases)
    subtitle = strip_brand_from_display(output.get("product_name_subtitle"), brand_name, aliases)
    tags = output.get("normalized_tags")
    if not isinstance(tags, list):
        tags = []
    tags = list(dict.fromkeys(_clean_space(item)[:64] for item in tags if _clean_space(item)))

    passed, hard_reason = hard_filter_candidate(display)
    is_candidate = bool(output.get("is_product_name_candidate"))
    reject_reason = _clean_space(output.get("reject_reason"))
    truncation = bool(output.get("truncation_suspected")) or _looks_truncated(display or raw_text)

    if candidate_type == "unknown" and display:
        candidate_type = infer_candidate_type(display, raw_text)
    if normalize_life_stage_display(display):
        candidate_type = "life_stage"
    if is_candidate and display and quality == "invalid" and candidate_type != "unknown":
        quality = (
            "strong"
            if candidate_type in {"model_code", "series_name", "official_name"}
            else "medium"
        )
        review_status = "needs_manual_review"

    if not display or not passed:
        is_candidate = False
        quality = "invalid"
        review_status = "rejected"
        reject_reason = reject_reason or hard_reason or "empty_display"
        display = ""

    if candidate_type == "life_stage":
        stage_source = display or raw_text
        stage_display = normalize_life_stage_display(stage_source)
        if not stage_display:
            stage_display = next(
                (
                    normalized
                    for alias, normalized in LIFE_STAGE_DISPLAY_MAP.items()
                    if _normalize_key(alias) in _normalize_key(stage_source)
                ),
                "",
            )
        if stage_display:
            display = stage_display
            is_candidate = True
            quality = "medium"
            review_status = "needs_manual_review"
            reject_reason = ""

    generic_weak = _normalize_key(display) in {
        _normalize_key(item) for item in GENERIC_WEAK_TERMS
    }
    if generic_weak or candidate_type in {"process_type", "function_position"}:
        if display and display not in tags:
            tags.append(display)
        subtitle = subtitle or display
        display = ""
        is_candidate = False
        quality = "weak"
        review_status = "needs_manual_review"
        reject_reason = reject_reason or "generic_function_process_or_life_stage"

    if candidate_type in {"feeding_instruction", "ingredient_text", "ocr_noise"}:
        display = ""
        is_candidate = False
        quality = "invalid"
        review_status = "rejected"
        reject_reason = reject_reason or candidate_type

    if truncation:
        review_status = "pending"
        if quality == "strong":
            quality = "medium"

    if quality == "strong" and candidate_type not in {
        "model_code",
        "series_name",
        "official_name",
    }:
        quality = "medium"

    return {
        "is_product_name_candidate": is_candidate,
        "product_name_display": display or None,
        "product_name_subtitle": subtitle or None,
        "candidate_type": candidate_type,
        "name_quality": quality,
        "review_status": review_status,
        "normalized_tags": tags,
        "truncation_suspected": truncation,
        "reject_reason": reject_reason or None,
        "reason": _clean_space(output.get("reason"))[:1000],
    }


class ProductNameModelClassifier:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        max_attempts: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError(
                "缺少大模型 API Key。请配置 QWEN_API_KEY/DASHSCOPE_API_KEY，"
                "或显式使用 --no-model。"
            )
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.max_attempts = max(1, max_attempts)

    def classify(
        self,
        *,
        raw_text: str,
        brand_name: str,
        known_brand_aliases: list[str],
        context_text: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = {
            "raw_text": raw_text,
            "brand_name_std": brand_name or None,
            "known_brand_aliases": known_brand_aliases,
            "context_text": context_text or None,
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False),
                        },
                    ],
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content
                raw_result = _extract_json_object(content)
                validated = validate_model_result(
                    raw_result,
                    raw_text=raw_text,
                    brand_name=brand_name,
                    aliases=known_brand_aliases,
                )
                return validated, raw_result
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(f"product name model failed: {last_error}") from last_error


def _split_lines(text: str) -> list[str]:
    lines: list[str] = []
    for part in re.split(r"[\n\r|]+", text or ""):
        cleaned = _clean_space(part).strip(" -—–:：,，;；")
        if not cleaned:
            continue
        if cleaned.lower() in NOISE_TERMS:
            continue
        lines.append(cleaned)
    return lines


def _load_brand_master() -> tuple[dict[str, dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    payload = yaml.safe_load(BRAND_MASTER_PATH.read_text(encoding="utf-8")) or {}
    by_key: dict[str, dict[str, Any]] = {}
    tokens: list[tuple[str, dict[str, Any]]] = []

    def add(value: Any, row: dict[str, Any]) -> None:
        key = _normalize_key(value)
        if not key:
            return
        by_key[key] = row
        tokens.append((str(value), row))

    for item in payload.get("brands") or []:
        if str(item.get("status") or "active") != "active":
            continue
        row = {
            "brand_id": int(item["brand_id"]),
            "standard_name": _clean_space(item.get("standard_name")),
        }
        add(row["standard_name"], row)
        for alias in item.get("aliases") or []:
            add(alias, row)
    for item in payload.get("alias_mappings") or []:
        standard = by_key.get(_normalize_key(item.get("standard_name")))
        if standard:
            add(item.get("alias"), standard)

    tokens.sort(key=lambda pair: len(_normalize_key(pair[0])), reverse=True)
    return by_key, tokens


def resolve_brand(
    parsed_brand: Any,
    ocr_text: str,
    brand_by_key: dict[str, dict[str, Any]],
    brand_tokens: list[tuple[str, dict[str, Any]]],
) -> tuple[int | None, str, str]:
    parsed = _clean_space(parsed_brand)
    parsed_key = _normalize_key(parsed)
    if parsed_key in brand_by_key:
        row = brand_by_key[parsed_key]
        return row["brand_id"], row["standard_name"], "parsed_exact"

    # Corrupted parsed brands frequently contain a valid brand plus product words.
    parsed_matches = [
        row
        for token, row in brand_tokens
        if _normalize_key(token) and _normalize_key(token) in parsed_key
    ]
    parsed_unique = {(row["brand_id"], row["standard_name"]) for row in parsed_matches}
    if len(parsed_unique) == 1:
        brand_id, name = next(iter(parsed_unique))
        return brand_id, name, "parsed_contains_alias"

    # OCR matching is a fallback because marketplace screenshots can mention
    # unrelated brands in navigation/recommendation text.
    head = _normalize_key((ocr_text or "")[:500])
    ocr_matches = [
        row
        for token, row in brand_tokens
        if len(_normalize_key(token)) >= 2 and _normalize_key(token) in head
    ]
    ocr_unique = {(row["brand_id"], row["standard_name"]) for row in ocr_matches}
    if len(ocr_unique) == 1:
        brand_id, name = next(iter(ocr_unique))
        return brand_id, name, "ocr_head_unique"
    return None, parsed if parsed and not parsed.startswith("未知品牌_") else "", "unmatched"


def _clean_product_phrase(value: Any, brand_name: str = "") -> str:
    text = _clean_space(value)
    text = LABEL_TAIL_RE.sub("", text)
    text = re.sub(r"^[：:：\-\s]+|[：:：\-\s]+$", "", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:kg|g|磅|斤|袋|罐)\b.*$", "", text, flags=re.I)
    text = re.sub(r"^(?:通用名称|商品名称|产品名称|品名)\s*[:：]?\s*", "", text)
    text = re.sub(r"(?:本产品符合.*|保质期.*|净含量.*)$", "", text)
    if brand_name:
        text = re.sub(re.escape(brand_name), "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -—–:：,，;；")
    return text[:160]


def _looks_product_like(line: str) -> bool:
    if not 2 <= len(line) <= 80:
        return False
    lowered = line.lower()
    if any(term in lowered for term in NOISE_TERMS):
        return False
    if re.search(
        r"(?:原料|添加剂|保证值|粗蛋白|粗脂肪|保质期|净含量|产品规格|"
        r"维生素|生育酚|吡哆醇|牛磺酸|氯化|硫酸|磷酸|烟酰胺|"
        r"\d+(?:\.\d+)?\s*%)",
        line,
    ):
        return False
    if re.fullmatch(r"[\d\s%./:+\-]+", line):
        return False
    return bool(
        re.search(
            r"猫粮|成猫|幼猫|全猫|配方|口味|系列|风干|烘焙|冻干|"
            r"呵护|护理|低敏|室内|绝育|[A-Za-z]{1,5}\d{1,4}",
            line,
            re.I,
        )
    )


def extract_standard_product_name(ocr_text: str, brand_name: str) -> tuple[str, str, list[str]]:
    reasons: list[str] = []
    for match in PRODUCT_LABEL_RE.finditer(ocr_text or ""):
        candidate = _clean_product_phrase(match.group(1), brand_name)
        if candidate and len(candidate) >= 2:
            reasons.append("explicit_product_label")
            return candidate, "explicit_label", reasons

    lines = _split_lines(ocr_text)
    candidates = [_clean_product_phrase(line, brand_name) for line in lines[:25] if _looks_product_like(line)]
    candidates = [item for item in candidates if item and _normalize_key(item) not in {_normalize_key(x) for x in GENERIC_PRODUCT_NAMES}]
    if candidates:
        candidates.sort(
            key=lambda item: (
                1 if "猫粮" in item else 0,
                1 if re.search(r"配方|口味|系列|风干|烘焙|冻干|呵护|低敏", item) else 0,
                -abs(len(item) - 14),
            ),
            reverse=True,
        )
        reasons.append("ocr_packaging_line")
        return candidates[0], "packaging_line", reasons

    reasons.append("no_reliable_product_phrase")
    return "", "none", reasons


def extract_model(ocr_text: str, product_name: str) -> str:
    header = (ocr_text or "")[:600]
    search_text = f"{product_name}\n{header}"
    candidates: list[tuple[str, int]] = []
    for pattern in MODEL_PATTERNS:
        for match in pattern.finditer(search_text):
            value = match.group(1)
            cleaned = re.sub(r"\s+", "", value).upper()
            key = cleaned.lower().replace("-", "")
            if key in MODEL_STOPWORDS:
                continue
            if MODEL_UNIT_RE.fullmatch(cleaned) or MODEL_NOISE_RE.search(cleaned):
                continue
            if re.fullmatch(r"[ABCDKE]\d{1,3}", cleaned):
                continue
            if "/" in cleaned:
                continue
            if re.fullmatch(r"(?:\d{1,2}G|[A-Z]\d{1,2})", cleaned) and cleaned in {"5G", "4G", "3G"}:
                continue
            if not (re.search(r"[A-Z]", cleaned) and re.search(r"\d", cleaned)):
                continue
            # A model code is strongest when it occurs in the extracted product
            # phrase. Otherwise require a short standalone code near the OCR head.
            score = 3 if _normalize_key(cleaned) in _normalize_key(product_name) else 1
            occurrences = len(re.findall(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", header, re.I))
            if score == 1 and (len(cleaned) > 8 or occurrences < 2):
                continue
            candidates.append((cleaned, score))
    if not candidates:
        return ""
    candidates.sort(key=lambda pair: (pair[1], -len(pair[0])), reverse=True)
    return candidates[0][0]


def _extract_first_pattern(text: str, patterns: Iterable[re.Pattern[str]]) -> str:
    for pattern in patterns:
        match = pattern.search(text or "")
        if match:
            return _clean_space(match.group(1))
    return ""


def extract_series(ocr_text: str, product_name: str) -> str:
    value = _extract_first_pattern(f"{product_name}\n{ocr_text}", SERIES_PATTERNS)
    if not value:
        return ""
    value = re.sub(r"^(?:产品系列|所属系列)\s*[:：]?\s*", "", value, flags=re.I)
    return value[:64]


def extract_function(ocr_text: str, product_name: str) -> str:
    header = re.split(
        r"(?:原料组成|配料组成|成分表|INGREDIENTS)",
        ocr_text or "",
        maxsplit=1,
        flags=re.I,
    )[0]
    haystack = f"{product_name}\n{header[:500]}".lower()
    for term in FUNCTION_TERMS:
        if term.lower() in haystack:
            return term
    return ""


def extract_process(ocr_text: str, product_name: str) -> str:
    header = re.split(
        r"(?:原料组成|配料组成|成分表|INGREDIENTS)",
        ocr_text or "",
        maxsplit=1,
        flags=re.I,
    )[0]
    haystack = f"{product_name}\n{header[:600]}".lower()
    for term in PROCESS_TERMS:
        if term.lower() in haystack:
            return term
    return ""


def extract_flavor_or_protein(ocr_text: str, product_name: str) -> str:
    before_ingredients = re.split(
        r"(?:原料组成|配料组成|成分表|INGREDIENTS)",
        ocr_text or "",
        maxsplit=1,
        flags=re.I,
    )[0]
    head = f"{product_name}\n{before_ingredients[:1000]}"
    flavor_match = re.search(r"(?:产品口味\s*[:：]?|口味\s*[:：])\s*([^\n|,，;；]{1,20})", head, re.I)
    if flavor_match:
        value = _clean_space(flavor_match.group(1))
        value = re.sub(r"(?:味|口味)$", "", value)
        if value and not re.search(r"(?:原料|组成|规格|保质期|净含量)", value):
            return value[:32]

    product_scope = product_name or head[:300]
    matches: list[str] = []
    for label, aliases in PROTEIN_ALIASES:
        if any(alias in product_scope for alias in aliases):
            matches.append(label)
        if len(matches) >= 2:
            break
    if len(matches) == 2 and matches[0] != matches[1]:
        return "".join(matches)
    return matches[0] if matches else ""


def extract_life_stage(ocr_text: str, product_name: str) -> str:
    haystack = f"{product_name}\n{(ocr_text or '')[:1600]}"
    matches = [label for label, pattern in LIFE_STAGE_PATTERNS if pattern.search(haystack)]
    if "全阶段" in matches:
        return "全阶段"
    if "幼猫" in matches and "成猫" in matches:
        return "全阶段"
    return matches[0] if matches else ""


def extract_product_type(ocr_text: str, product_name: str) -> str:
    haystack = f"{product_name}\n{(ocr_text or '')[:1000]}"
    for label, pattern in PRODUCT_TYPE_PATTERNS:
        if pattern.search(haystack):
            return label
    return ""


def clean_short_product_name(product_name: str, brand_name: str) -> str:
    text = _clean_product_phrase(product_name, brand_name)
    if _normalize_key(text) in {_normalize_key(item) for item in GENERIC_PRODUCT_NAMES}:
        return text[:40] or "待确认产品"
    text = re.sub(r"^(?:全价|宠物食品|宠物配合饲料)+", "", text)
    text = re.sub(r"(?:全价宠物食品)?(?:成年期|幼年期)?猫粮$", "", text)
    text = re.sub(r"(?:配方|口味)$", "", text)
    text = text.strip(" -—–:：,，;；()（）")
    return text[:40] or "待确认产品"


def choose_display_name(
    *,
    model_name: str,
    series_name: str,
    function_name: str,
    process_name: str,
    flavor_or_protein: str,
    life_stage: str,
    standard_product_name: str,
    brand_name: str,
) -> tuple[str, str, str]:
    if model_name:
        return model_name, "model", "strong"
    if series_name:
        return series_name, "series", "strong"
    if function_name:
        return function_name, "function", "medium"
    if process_name:
        return f"{process_name}猫粮", "process", "medium"
    if flavor_or_protein:
        suffix = "成猫粮" if life_stage == "成猫" else "猫粮"
        return f"{flavor_or_protein}{suffix}", "flavor_or_protein", "medium"
    if life_stage:
        display = LIFE_STAGE_DISPLAY_MAP.get(life_stage)
        if display:
            return display, "life_stage", "medium"
    return clean_short_product_name(standard_product_name, brand_name), "clean_short_name", "weak"


def build_rule_fallback_result(
    *,
    product_name: str,
    brand_name: str,
    model_name: str,
    series_name: str,
    function_name: str,
    process_name: str,
    flavor_or_protein: str,
    life_stage: str,
) -> dict[str, Any]:
    display, display_rule, quality = choose_display_name(
        model_name=model_name,
        series_name=series_name,
        function_name=function_name,
        process_name=process_name,
        flavor_or_protein=flavor_or_protein,
        life_stage=life_stage,
        standard_product_name=product_name,
        brand_name=brand_name,
    )
    type_map = {
        "model": "model_code",
        "series": "series_name",
        "function": "function_position",
        "process": "process_type",
        "flavor_or_protein": "flavor_protein",
        "life_stage": "life_stage",
        "clean_short_name": "official_name",
    }
    return validate_model_result(
        {
            "is_product_name_candidate": bool(display),
            "product_name_display": display,
            "product_name_subtitle": None,
            "candidate_type": type_map[display_rule],
            "name_quality": quality,
            "review_status": "needs_manual_review",
            "normalized_tags": [
                item
                for item in (function_name, process_name, life_stage)
                if item
            ],
            "truncation_suspected": _looks_truncated(product_name),
            "reject_reason": None,
            "reason": "rule_fallback",
        },
        raw_text=product_name,
        brand_name=brand_name,
        aliases=[],
    )


def build_candidate(
    row: dict[str, Any],
    brand_by_key: dict[str, dict[str, Any]],
    brand_tokens: list[tuple[str, dict[str, Any]]],
    classifier: ProductNameModelClassifier | None = None,
) -> ProductCandidate:
    ocr_text = str(row.get("ocr_text") or "")
    brand_id, brand_name, brand_method = resolve_brand(
        row.get("parsed_brand"), ocr_text, brand_by_key, brand_tokens
    )
    product_name, product_method, reasons = extract_standard_product_name(ocr_text, brand_name)

    model_name = extract_model(ocr_text, product_name)
    series_name = extract_series(ocr_text, product_name)
    function_name = extract_function(ocr_text, product_name)
    process_name = extract_process(ocr_text, product_name)
    flavor_or_protein = extract_flavor_or_protein(ocr_text, product_name)
    life_stage = extract_life_stage(ocr_text, product_name)
    product_type = extract_product_type(ocr_text, product_name)
    if not product_name:
        product_name = clean_short_product_name(row.get("parsed_product_name"), brand_name)
        reasons.append("product_name_rule_fallback")

    passed_hard_filter, hard_filter_reason = hard_filter_candidate(product_name)
    aliases = brand_aliases_for(brand_id, brand_tokens)
    model_raw_result: dict[str, Any] = {}
    if not passed_hard_filter:
        decision = {
            "is_product_name_candidate": False,
            "product_name_display": None,
            "product_name_subtitle": None,
            "candidate_type": "ocr_noise",
            "name_quality": "invalid",
            "review_status": "rejected",
            "normalized_tags": [],
            "truncation_suspected": False,
            "reject_reason": hard_filter_reason,
            "reason": "hard_rule_rejected_before_model",
        }
        display_rule = "hard_reject"
    elif classifier is not None:
        try:
            decision, model_raw_result = classifier.classify(
                raw_text=product_name,
                brand_name=brand_name,
                known_brand_aliases=aliases,
                context_text=build_context_text(ocr_text, product_name),
            )
            display_rule = f"model:{decision['candidate_type']}"
        except Exception as exc:
            decision = build_rule_fallback_result(
                product_name=product_name,
                brand_name=brand_name,
                model_name=model_name,
                series_name=series_name,
                function_name=function_name,
                process_name=process_name,
                flavor_or_protein=flavor_or_protein,
                life_stage=life_stage,
            )
            decision["review_status"] = "needs_manual_review"
            decision["reason"] = f"model_error_rule_fallback: {exc}"
            display_rule = "model_error_rule_fallback"
            reasons.append("model_error")
    else:
        decision = build_rule_fallback_result(
            product_name=product_name,
            brand_name=brand_name,
            model_name=model_name,
            series_name=series_name,
            function_name=function_name,
            process_name=process_name,
            flavor_or_protein=flavor_or_protein,
            life_stage=life_stage,
        )
        display_rule = f"rule:{decision['candidate_type']}"

    display_name = _clean_space(decision.get("product_name_display"))
    display_subtitle = _clean_space(decision.get("product_name_subtitle"))
    quality = str(decision.get("name_quality") or "invalid")
    if brand_id is None:
        if quality == "strong":
            quality = "medium"
        reasons.append("brand_unmatched")
    reasons.extend([f"brand:{brand_method}", f"product:{product_method}", f"display:{display_rule}"])

    return ProductCandidate(
        brand_id=brand_id,
        standard_brand_name=brand_name,
        standard_product_name=product_name,
        display_name=display_name,
        display_name_rule=display_rule,
        display_subtitle=display_subtitle,
        candidate_type=str(decision.get("candidate_type") or "unknown"),
        review_status=str(decision.get("review_status") or "needs_manual_review"),
        normalized_tags=list(decision.get("normalized_tags") or []),
        truncation_suspected=bool(decision.get("truncation_suspected")),
        reject_reason=_clean_space(decision.get("reject_reason")),
        model_reason=_clean_space(decision.get("reason")),
        hard_filter_reason=hard_filter_reason or "",
        model_name_used=classifier.model if classifier else "",
        model_prompt_version=PROMPT_VERSION if classifier else "",
        model_raw_result=model_raw_result,
        model_name=model_name,
        series_name=series_name,
        function_name=function_name,
        process_name=process_name,
        flavor_or_protein=flavor_or_protein,
        product_type=product_type,
        life_stage=life_stage,
        quality_level=quality,
        quality_reasons=reasons,
        source_ids=[int(row["source_id"])],
        parsed_row_ids=[int(row["parsed_row_id"])],
        raw_product_names=[_clean_space(row.get("parsed_product_name"))],
        evidence={
            "brand_method": brand_method,
            "product_method": product_method,
            "ocr_excerpt": _clean_space(ocr_text)[:500],
            "context_text": build_context_text(ocr_text, product_name),
        },
    )


def _candidate_key(candidate: ProductCandidate) -> str:
    identity = (
        candidate.model_name
        or candidate.series_name
        or candidate.display_name
        or candidate.standard_product_name
    )
    return f"{candidate.brand_id or candidate.standard_brand_name}|{_normalize_key(identity)}"


def merge_candidates(candidates: Iterable[ProductCandidate]) -> list[ProductCandidate]:
    grouped: dict[str, list[ProductCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[_candidate_key(candidate)].append(candidate)

    merged: list[ProductCandidate] = []
    for items in grouped.values():
        items.sort(
            key=lambda item: (
                QUALITY_RANK.get(item.quality_level, 0),
                len(item.standard_product_name),
            ),
            reverse=True,
        )
        best = items[0]
        for item in items[1:]:
            best.source_ids.extend(item.source_ids)
            best.parsed_row_ids.extend(item.parsed_row_ids)
            best.raw_product_names.extend(item.raw_product_names)
            best.quality_reasons.extend(item.quality_reasons)
        best.source_ids = sorted(set(best.source_ids))
        best.parsed_row_ids = sorted(set(best.parsed_row_ids))
        best.raw_product_names = sorted({x for x in best.raw_product_names if x})
        best.quality_reasons = sorted(set(best.quality_reasons))
        best.evidence["merged_row_count"] = len(items)
        merged.append(best)

    return sorted(
        merged,
        key=lambda item: (
            item.brand_id is None,
            item.brand_id or 999999,
            item.display_name,
            item.standard_product_name,
        ),
    )


def fetch_rows(
    conn: pymysql.connections.Connection,
    *,
    source_table: str,
    parsed_table: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    source_table = _safe_table(source_table)
    parsed_table = _safe_table(parsed_table)
    limit_sql = "LIMIT %s" if limit else ""
    params: tuple[Any, ...] = (int(limit),) if limit else ()
    sql = f"""
        SELECT
            p.id AS parsed_row_id,
            p.source_id,
            p.brand AS parsed_brand,
            p.product_name AS parsed_product_name,
            s.ocr_text
        FROM `{parsed_table}` p
        JOIN `{source_table}` s ON s.id = p.source_id
        WHERE s.ocr_text IS NOT NULL
          AND TRIM(s.ocr_text) <> ''
        ORDER BY p.id
        {limit_sql}
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def ensure_target_table(conn: pymysql.connections.Connection, table_name: str) -> None:
    table_name = _safe_table(table_name)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{table_name}` (
              product_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              brand_id BIGINT NULL,
              standard_brand_name VARCHAR(255) NULL,
              standard_product_name VARCHAR(512) NOT NULL,
              display_name VARCHAR(255) NULL,
              display_name_rule VARCHAR(32) NOT NULL,
              display_subtitle VARCHAR(255) NULL,
              candidate_type VARCHAR(32) NOT NULL DEFAULT 'unknown',
              model_name VARCHAR(64) NULL,
              series_name VARCHAR(128) NULL,
              function_name VARCHAR(128) NULL,
              process_name VARCHAR(64) NULL,
              flavor_or_protein VARCHAR(128) NULL,
              product_country VARCHAR(128) NULL,
              price_band VARCHAR(64) NULL,
              product_type VARCHAR(64) NULL,
              life_stage VARCHAR(64) NULL,
              product_image VARCHAR(1024) NULL,
              active TINYINT NOT NULL DEFAULT 0,
              quality_level VARCHAR(16) NOT NULL,
              review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
              normalized_tags_json JSON NULL,
              truncation_suspected TINYINT NOT NULL DEFAULT 0,
              reject_reason VARCHAR(512) NULL,
              model_reason TEXT NULL,
              hard_filter_reason VARCHAR(128) NULL,
              model_name_used VARCHAR(128) NULL,
              model_prompt_version VARCHAR(64) NULL,
              model_raw_result_json JSON NULL,
              quality_reasons_json JSON NULL,
              source_ids_json JSON NOT NULL,
              parsed_row_ids_json JSON NOT NULL,
              raw_product_names_json JSON NULL,
              evidence_json JSON NULL,
              build_batch_id CHAR(32) NOT NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (product_id),
              KEY idx_brand_id (brand_id),
              KEY idx_display_name (display_name),
              KEY idx_quality_review (quality_level, review_status),
              UNIQUE KEY uq_brand_product (brand_id, standard_product_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
        columns = {row["Field"] for row in cursor.fetchall()}
        additions = {
            "display_subtitle": "VARCHAR(255) NULL AFTER display_name_rule",
            "candidate_type": "VARCHAR(32) NOT NULL DEFAULT 'unknown' AFTER display_subtitle",
            "normalized_tags_json": "JSON NULL AFTER review_status",
            "truncation_suspected": "TINYINT NOT NULL DEFAULT 0 AFTER normalized_tags_json",
            "reject_reason": "VARCHAR(512) NULL AFTER truncation_suspected",
            "model_reason": "TEXT NULL AFTER reject_reason",
            "hard_filter_reason": "VARCHAR(128) NULL AFTER model_reason",
            "model_name_used": "VARCHAR(128) NULL AFTER hard_filter_reason",
            "model_prompt_version": "VARCHAR(64) NULL AFTER model_name_used",
            "model_raw_result_json": "JSON NULL AFTER model_prompt_version",
        }
        for column, definition in additions.items():
            if column not in columns:
                cursor.execute(
                    f"ALTER TABLE `{table_name}` ADD COLUMN `{column}` {definition}"
                )
        cursor.execute(
            f"ALTER TABLE `{table_name}` MODIFY COLUMN display_name VARCHAR(255) NULL"
        )


def write_candidates(
    conn: pymysql.connections.Connection,
    table_name: str,
    candidates: list[ProductCandidate],
    *,
    replace: bool,
) -> str:
    table_name = _safe_table(table_name)
    ensure_target_table(conn, table_name)
    batch_id = uuid.uuid4().hex
    with conn.cursor() as cursor:
        if replace:
            cursor.execute(f"TRUNCATE TABLE `{table_name}`")
        sql = f"""
            INSERT INTO `{table_name}` (
              brand_id, standard_brand_name, standard_product_name, display_name,
              display_name_rule, display_subtitle, candidate_type,
              model_name, series_name, function_name, process_name,
              flavor_or_protein, product_type, life_stage, active, quality_level,
              review_status, normalized_tags_json, truncation_suspected, reject_reason,
              model_reason, hard_filter_reason, model_name_used, model_prompt_version,
              model_raw_result_json, quality_reasons_json, source_ids_json, parsed_row_ids_json,
              raw_product_names_json, evidence_json, build_batch_id
            ) VALUES (
              %(brand_id)s, %(standard_brand_name)s, %(standard_product_name)s, %(display_name)s,
              %(display_name_rule)s, %(display_subtitle)s, %(candidate_type)s,
              %(model_name)s, %(series_name)s, %(function_name)s, %(process_name)s,
              %(flavor_or_protein)s, %(product_type)s, %(life_stage)s, 0, %(quality_level)s,
              %(review_status)s, %(normalized_tags_json)s, %(truncation_suspected)s, %(reject_reason)s,
              %(model_reason)s, %(hard_filter_reason)s, %(model_name_used)s, %(model_prompt_version)s,
              %(model_raw_result_json)s, %(quality_reasons_json)s, %(source_ids_json)s, %(parsed_row_ids_json)s,
              %(raw_product_names_json)s, %(evidence_json)s, %(build_batch_id)s
            )
            ON DUPLICATE KEY UPDATE
              display_name = VALUES(display_name),
              display_name_rule = VALUES(display_name_rule),
              display_subtitle = VALUES(display_subtitle),
              candidate_type = VALUES(candidate_type),
              model_name = VALUES(model_name),
              series_name = VALUES(series_name),
              function_name = VALUES(function_name),
              process_name = VALUES(process_name),
              flavor_or_protein = VALUES(flavor_or_protein),
              product_type = VALUES(product_type),
              life_stage = VALUES(life_stage),
              quality_level = VALUES(quality_level),
              review_status = VALUES(review_status),
              normalized_tags_json = VALUES(normalized_tags_json),
              truncation_suspected = VALUES(truncation_suspected),
              reject_reason = VALUES(reject_reason),
              model_reason = VALUES(model_reason),
              hard_filter_reason = VALUES(hard_filter_reason),
              model_name_used = VALUES(model_name_used),
              model_prompt_version = VALUES(model_prompt_version),
              model_raw_result_json = VALUES(model_raw_result_json),
              quality_reasons_json = VALUES(quality_reasons_json),
              source_ids_json = VALUES(source_ids_json),
              parsed_row_ids_json = VALUES(parsed_row_ids_json),
              raw_product_names_json = VALUES(raw_product_names_json),
              evidence_json = VALUES(evidence_json),
              build_batch_id = VALUES(build_batch_id),
              updated_at = NOW()
        """
        payload = []
        for item in candidates:
            payload.append(
                {
                    "brand_id": item.brand_id,
                    "standard_brand_name": item.standard_brand_name or None,
                    "standard_product_name": item.standard_product_name,
                    "display_name": item.display_name or None,
                    "display_name_rule": item.display_name_rule,
                    "display_subtitle": item.display_subtitle or None,
                    "candidate_type": item.candidate_type,
                    "model_name": item.model_name or None,
                    "series_name": item.series_name or None,
                    "function_name": item.function_name or None,
                    "process_name": item.process_name or None,
                    "flavor_or_protein": item.flavor_or_protein or None,
                    "product_type": item.product_type or None,
                    "life_stage": item.life_stage or None,
                    "quality_level": item.quality_level,
                    "review_status": item.review_status,
                    "normalized_tags_json": json.dumps(item.normalized_tags, ensure_ascii=False),
                    "truncation_suspected": int(item.truncation_suspected),
                    "reject_reason": item.reject_reason or None,
                    "model_reason": item.model_reason or None,
                    "hard_filter_reason": item.hard_filter_reason or None,
                    "model_name_used": item.model_name_used or None,
                    "model_prompt_version": item.model_prompt_version or None,
                    "model_raw_result_json": json.dumps(item.model_raw_result, ensure_ascii=False),
                    "quality_reasons_json": json.dumps(item.quality_reasons, ensure_ascii=False),
                    "source_ids_json": json.dumps(item.source_ids, ensure_ascii=False),
                    "parsed_row_ids_json": json.dumps(item.parsed_row_ids, ensure_ascii=False),
                    "raw_product_names_json": json.dumps(item.raw_product_names, ensure_ascii=False),
                    "evidence_json": json.dumps(item.evidence, ensure_ascii=False),
                    "build_batch_id": batch_id,
                }
            )
        cursor.executemany(sql, payload)
    conn.commit()
    return batch_id


def export_csv(path: Path, candidates: list[ProductCandidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "brand_id",
        "standard_brand_name",
        "standard_product_name",
        "display_name",
        "display_name_rule",
        "display_subtitle",
        "candidate_type",
        "model_name",
        "series_name",
        "function_name",
        "process_name",
        "flavor_or_protein",
        "product_type",
        "life_stage",
        "quality_level",
        "review_status",
        "normalized_tags",
        "truncation_suspected",
        "reject_reason",
        "model_reason",
        "hard_filter_reason",
        "source_ids",
        "raw_product_names",
        "quality_reasons",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in candidates:
            row = item.__dict__.copy()
            for key in (
                "source_ids",
                "raw_product_names",
                "quality_reasons",
                "normalized_tags",
            ):
                row[key] = json.dumps(row[key], ensure_ascii=False)
            writer.writerow({key: row.get(key) for key in fields})


def print_summary(candidates: list[ProductCandidate], scanned: int) -> None:
    quality_counts = defaultdict(int)
    rule_counts = defaultdict(int)
    review_counts = defaultdict(int)
    for item in candidates:
        quality_counts[item.quality_level] += 1
        rule_counts[item.display_name_rule] += 1
        review_counts[item.review_status] += 1
    print(
        json.dumps(
            {
                "scanned_rows": scanned,
                "candidate_count": len(candidates),
                "quality_counts": dict(sorted(quality_counts.items())),
                "display_rule_counts": dict(sorted(rule_counts.items())),
                "review_status_counts": dict(sorted(review_counts.items())),
                "unmatched_brand_count": sum(1 for item in candidates if item.brand_id is None),
                "empty_display_count": sum(1 for item in candidates if not item.display_name),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-table", default=DEFAULT_SOURCE_TABLE)
    parser.add_argument("--parsed-table", default=DEFAULT_PARSED_TABLE)
    parser.add_argument("--target-table", default=DEFAULT_TARGET_TABLE)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument(
        "--no-model",
        action="store_true",
        help="skip LLM classification and use deterministic fallback rules",
    )
    parser.add_argument("--model", help="override configured Qwen text model")
    parser.add_argument("--model-max-attempts", type=int, default=3)
    parser.add_argument("--apply", action="store_true", help="write candidates to MySQL")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="truncate the candidate table before writing; only valid with --apply",
    )
    args = parser.parse_args()
    if args.replace and not args.apply:
        parser.error("--replace requires --apply")

    cfg = get_mysql_config()
    conn = pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    try:
        brand_by_key, brand_tokens = _load_brand_master()
        classifier: ProductNameModelClassifier | None = None
        if not args.no_model:
            qwen = get_qwen_config({"model": args.model} if args.model else None)
            classifier = ProductNameModelClassifier(
                api_key=qwen["api_key"],
                base_url=qwen["base_url"],
                model=qwen["model"],
                max_attempts=args.model_max_attempts,
            )
        rows = fetch_rows(
            conn,
            source_table=args.source_table,
            parsed_table=args.parsed_table,
            limit=args.limit,
        )
        candidates = merge_candidates(
            build_candidate(row, brand_by_key, brand_tokens, classifier) for row in rows
        )
        print_summary(candidates, len(rows))
        if args.output_csv:
            export_csv(args.output_csv, candidates)
            print(f"csv={args.output_csv}")
        if args.apply:
            batch_id = write_candidates(
                conn,
                args.target_table,
                candidates,
                replace=args.replace,
            )
            print(f"table={args.target_table} batch_id={batch_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
