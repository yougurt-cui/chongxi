# -*- coding: utf-8 -*-
import json
import os
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Tuple, Optional

import pymysql


# =========================================================
# 1. 数据库配置
# =========================================================
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "protein_feature_platform"),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
    "cursorclass": pymysql.cursors.DictCursor,
}

SOURCE_TABLE = "catfood_fiber_feature_json"
TARGET_TABLE = "catfood_fiber_feature_score"
STANDARD_DB = os.getenv("CATFOOD_STANDARD_DB", "csv_labeling")
LEGACY_COMBO_TABLE = "catfood_fiber_product_combo_result"
MAIN_COMPONENT_CUMULATIVE_SHARE = 0.70
MIN_COMPONENT_SCORE = 0.05
MAX_MAIN_COMPONENT_COUNT = 2
POSITION_WEIGHT_RULES = [(1, 1, 1.2), (2, 3, 1.0), (4, 5, 0.8),
                         (6, 8, 0.6), (9, 12, 0.4), (13, 9999, 0.2)]
UNKNOWN_POSITION_WEIGHT = 0.5


# =========================================================
# 2. 建表 SQL
# =========================================================
CREATE_TARGET_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    formula_id BIGINT UNSIGNED NULL,
    source_id BIGINT NULL,
    product_key VARCHAR(600) NOT NULL,
    brand VARCHAR(255),
    product_name VARCHAR(255),
    starch_burden_score DECIMAL(12,4),
    p_form_score DECIMAL(12,4),
    p_bulk_score DECIMAL(12,4),
    p_buffer_score DECIMAL(12,4),
    p_total_score DECIMAL(12,4),
    q_feed_score DECIMAL(12,4),
    q_scfa_score DECIMAL(12,4),
    q_total_score DECIMAL(12,4),
    p_level VARCHAR(10),
    q_level VARCHAR(10),
    pq_combo_name VARCHAR(50),
    p_main_ingredients TEXT,
    q_main_ingredients TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uk_formula_id (formula_id),
    KEY idx_source_id (source_id),
    KEY idx_brand (brand),
    KEY idx_product_name (product_name)
);
"""


# =========================================================
# 3. 别名映射
# 作用：把标签 key 对齐到真实配方原料
# 你后面可以持续扩充
# =========================================================
ALIAS_MAP = {
    "菊粉": ["菊苣根干", "菊苣根", "菊苣纤维", "菊粉"],
    "菊苣菊糖": ["菊苣菊糖", "菊糖", "菊苣", "菊糖（FOS）", "果寡糖", "FOS"],
    "蔓越莓": ["蔓越莓干", "蔓越莓"],
    "海带干": ["海带干", "海带"],
    "豌豆纤维": ["豌豆纤维"],
    "甜菜粕": ["甜菜粕", "甜菜浆", "干甜菜浆", "脱水甜菜浆", "甜菜纤维", "甜辣粕"],
    "车前子壳": ["车前子壳", "车前子", "洋车前子壳", "洋车前子", "车前子粉", "车前子壳粉"],
    "苜蓿草颗粒": ["苜蓿草颗粒", "苜宿草颗粒", "苜蓿干草", "苜蓿草", "苜蓿"],
    "FOS": ["FOS", "果寡糖", "菊糖（FOS）", "低聚果糖"],
    "凝结芽孢杆菌": ["凝结芽孢杆菌"],
    "丝兰": ["天然类固醇萨酒皂角苷（源自丝兰）", "丝兰", "丝兰提取物"],
}


# =========================================================
# 4.1 淀粉负担规则
# 分值含义：单个原料的淀粉/碳水负担基础强度，最终会乘以配料顺位权重。
# =========================================================
STARCH_EXCLUDE_KEYWORDS = [
    "纤维", "蛋白", "油", "提取物", "果寡糖", "低聚果糖", "菊粉", "菊糖",
    "酵母", "维生素", "矿物质", "益生菌",
]

STARCH_LEVEL_SCORES = {1: 1.2, 2: 1.3, 3: 1.5, 4: 1.8, 5: 2.0}

STARCH_BASE_RULES = [
    {
        "category": "精制淀粉/纯淀粉",
        "level": 5,
        "keywords": [
            "玉米淀粉", "小麦淀粉", "马铃薯淀粉", "木薯淀粉",
            "豌豆淀粉", "变性淀粉", "淀粉"
        ],
    },
    {
        "category": "高淀粉粉类",
        "level": 4,
        "keywords": [
            "木薯粉", "马铃薯粉", "土豆粉", "玉米粉",
            "小麦粉", "大米粉", "米粉"
        ],
    },
    {
        "category": "薯类淀粉来源",
        "level": 3,
        "keywords": [
            "木薯", "马铃薯", "土豆", "红薯", "甘薯", "紫薯", "地瓜"
        ],
    },
    {
        "category": "谷物淀粉来源",
        "level": 2,
        "keywords": [
            "碎米", "大米", "白米", "糙米", "酿酒米", "酿造米", "燕麦", "小麦", "玉米", "高粱",
            "大麦", "小米", "藜麦"
        ],
    },
    {
        "category": "豆类碳水来源",
        "level": 1,
        "keywords": [
            "豌豆", "鹰嘴豆", "扁豆", "绿豆", "蚕豆"
        ],
    },
]


# =========================================================
# 4. 排位权重
# =========================================================
def rank_weight(rank):
    if rank is None:
        return UNKNOWN_POSITION_WEIGHT
    for start, end, weight in POSITION_WEIGHT_RULES:
        if start <= int(rank) <= end:
            return weight
    return UNKNOWN_POSITION_WEIGHT


def get_rank_weight(rank_index_1_based: int) -> float:
    return rank_weight(rank_index_1_based)


# =========================================================
# 5. Version B-lite 规则
# =========================================================
FIBER_FUNCTION_RULES = {
    "吸水成形": {"p_form": 1.0},
    "增加粪便骨架": {"p_bulk": 1.0},
    "缓冲刺激": {"p_buffer": 1.0},
    "稀释刺激物": {"p_buffer": 0.8},
    "功能性缓冲": {"p_buffer": 0.3},
    "温和供菌底物": {"q_feed": 0.4, "g": 0.2},
}

PREBIOTIC_FUNCTION_RULES = {
    "供菌": {"q_feed": 1.0, "g": 0.5},
    "促进有益菌增殖": {"q_feed": 1.0, "g": 0.3},
    "SCFA支持": {"q_scfa": 1.0, "g": 0.2},
}

FERMENTABILITY_LEVELS = {"低": 1, "中低": 2, "中": 3, "高": 4}
FERMENTABILITY_LEVEL_SCORES = {
    1: {"q_feed": 0.1, "q_scfa": 0.0, "g": 0.0},
    2: {"q_feed": 0.3, "q_scfa": 0.2, "g": 0.2},
    3: {"q_feed": 0.5, "q_scfa": 0.4, "g": 0.5},
    4: {"q_feed": 0.8, "q_scfa": 0.6, "g": 1.0},
}
FERMENTABILITY_RULES = {
    label: FERMENTABILITY_LEVEL_SCORES[level]
    for label, level in FERMENTABILITY_LEVELS.items()
}

FIBER_SOLUBILITY_LEVELS = {"不可溶": 1, "混合": 2, "可溶": 3}
FIBER_SOLUBILITY_LEVEL_SCORES = {
    1: {"p_bulk": 0.8, "p_form": 0.2},
    2: {"p_form": 0.4, "p_bulk": 0.4, "p_buffer": 0.2},
    3: {"p_form": 0.8},
}
FIBER_SOLUBILITY_RULES = {
    label: FIBER_SOLUBILITY_LEVEL_SCORES[level]
    for label, level in FIBER_SOLUBILITY_LEVELS.items()
}

# Thresholds are expressed on the normalized 0-100 business scale.
BUSINESS_LEVEL_THRESHOLDS = {"low_upper": 40.0, "high_lower": 60.0}
FIBER_DISPLAY_SCALE_MAX = 5.0

INGREDIENT_CATEGORY_RULES = {
    "益生元": {"q_feed": 0.6, "q_scfa": 0.4, "g": 0.3},
    "膳食纤维": {"p_bulk": 0.2, "p_buffer": 0.1},
    "益生菌": {"q_feed": 1.0, "g": 0.1},
}


STRUCTURAL_BULK_TAGS = {
    "纤维素",
    "竹纤维",
    "豌豆纤维",
    "豆纤维",
    "豌豆壳纤维",
    "大豆纤维",
    "鹰嘴豆纤维",
}

PORTRAIT_FUNCTION_SCORE_NAMES = ["吸水成形", "温和供菌底物", "缓冲刺激", "功能性缓冲"]

FUNCTION_SCORE_TARGETS = {
    "吸水成形": ("p_form",),
    "温和供菌底物": ("q_feed",),
    "缓冲刺激": ("p_buffer",),
    "功能性缓冲": ("p_buffer",),
}


# =========================================================
# 6. 基础工具函数
# =========================================================
def round4(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def build_product_key(brand: Optional[str], product_name: Optional[str]) -> str:
    return f"{(brand or '').strip()}||{(product_name or '').strip()}"


def normalize_source_ids(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return normalize_source_ids(parsed)
    return []


def parse_source_id(source_ids: Any, fallback_id: Any) -> Optional[int]:
    candidates = normalize_source_ids(source_ids) or [fallback_id]
    for item in candidates:
        if isinstance(item, dict):
            item = item.get("source_id") or item.get("id")
        try:
            return int(item)
        except (TypeError, ValueError):
            continue
    try:
        return int(fallback_id)
    except (TypeError, ValueError):
        return None


def normalize_json_field(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}

    return {}


def split_ingredients(raw_ingredient_text: str) -> List[str]:
    """
    拆配方原料顺序
    默认按中文逗号、英文逗号、分号、顿号切分
    """
    if not raw_ingredient_text:
        return []

    parts = re.split(r"[，,；;、]", raw_ingredient_text)
    return [p.strip() for p in parts if p and p.strip()]


def simplify_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"[()（）]", "", text)
    text = re.sub(r"(干|粉|提取物|纤维)$", "", text)
    return text.strip()


def normalize_ingredient_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\([^)]*\)", "", text)
    text = re.sub(r"\d+(?:\.\d+)?\s*%", "", text)
    text = re.sub(r"\s+", "", text)
    return text.strip()


def contains_any(text: str, keywords: List[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def classify_starch_ingredient(ingredient: str) -> Optional[Dict[str, Any]]:
    """
    识别配料表中的淀粉/碳水负担来源，并给出单原料基础分。
    显式“淀粉/粉/薯/谷物/豆类整料”会计入；纤维、蛋白、油脂等形态排除。
    """
    normalized = normalize_ingredient_text(ingredient)
    if not normalized:
        return None

    explicit_starch = "淀粉" in normalized
    if not explicit_starch and contains_any(normalized, STARCH_EXCLUDE_KEYWORDS):
        return None

    for rule in STARCH_BASE_RULES:
        matched_keywords = [
            keyword for keyword in rule["keywords"]
            if keyword in normalized
        ]
        if matched_keywords:
            return {
                "category": rule["category"],
                "level": int(rule["level"]),
                "base_score": float(STARCH_LEVEL_SCORES[int(rule["level"])]),
                "matched_keywords": matched_keywords,
            }

    return None


def calc_starch_burden(ingredient_list: List[str]) -> Tuple[float, List[Dict[str, Any]]]:
    """
    产品淀粉负担分 = Σ(淀粉原料基础分 * 配料顺位权重)
    """
    details = []
    total_score = 0.0

    for rank, ingredient in enumerate(ingredient_list, start=1):
        starch_info = classify_starch_ingredient(ingredient)
        if not starch_info:
            continue

        weight = get_rank_weight(rank)
        weighted = round4(starch_info["base_score"] * weight)
        total_score += weighted

        details.append({
            "ingredient_name": ingredient,
            "rank": rank,
            "weight": weight,
            "category": starch_info["category"],
            "matched_keywords": starch_info["matched_keywords"],
            "base_score": starch_info["base_score"],
            "weighted_score": weighted,
        })

    return round4(total_score), details


def find_best_match_rank(tag_name: str, ingredient_list: List[str]) -> Tuple[Optional[str], Optional[int]]:
    """
    根据 tag_name 在配方原料列表中找匹配项与顺位
    """
    alias_candidates = ALIAS_MAP.get(tag_name, [tag_name])

    # 第一轮：直接包含匹配
    for alias in alias_candidates:
        for idx, ing in enumerate(ingredient_list, start=1):
            if alias in ing or ing in alias:
                return ing, idx

    # 第二轮：简化匹配
    simple_aliases = [simplify_text(a) for a in alias_candidates]
    for sa in simple_aliases:
        for idx, ing in enumerate(ingredient_list, start=1):
            sing = simplify_text(ing)
            if sa and (sa in sing or sing in sa):
                return ing, idx

    return None, None


def should_skip_score_match(tag_name: str, matched_ingredient: Optional[str]) -> bool:
    tag = str(tag_name or "").strip()
    ingredient = str(matched_ingredient or "").strip()
    if tag == "南瓜" and "南瓜籽" in ingredient:
        return True
    return False


def apply_rule(score: Dict[str, float], rule: Dict[str, float]) -> None:
    for k, v in rule.items():
        score[k] = score.get(k, 0.0) + float(v)


def calc_single_ingredient_base_score(tag_detail: Dict[str, Any]) -> Dict[str, float]:
    """
    计算单个命中标签原料的基础分（未乘顺位权重）
    """
    score = {
        "p_form": 0.0,
        "p_bulk": 0.0,
        "p_buffer": 0.0,
        "q_feed": 0.0,
        "q_scfa": 0.0,
        "g": 0.0,
    }

    fiber_functions = tag_detail.get("fiber_functions") or []
    prebiotic_functions = tag_detail.get("prebiotic_functions") or []
    fermentability = tag_detail.get("fermentability")
    fiber_solubility = tag_detail.get("fiber_solubility")
    ingredient_category = tag_detail.get("ingredient_category")

    for func in fiber_functions:
        rule = FIBER_FUNCTION_RULES.get(func)
        if rule:
            apply_rule(score, rule)

    for func in prebiotic_functions:
        rule = PREBIOTIC_FUNCTION_RULES.get(func)
        if rule:
            apply_rule(score, rule)

    if fermentability in FERMENTABILITY_RULES:
        apply_rule(score, FERMENTABILITY_RULES[fermentability])

    if fiber_solubility in FIBER_SOLUBILITY_RULES:
        apply_rule(score, FIBER_SOLUBILITY_RULES[fiber_solubility])

    if ingredient_category in INGREDIENT_CATEGORY_RULES:
        apply_rule(score, INGREDIENT_CATEGORY_RULES[ingredient_category])

    return score


def weighted_score(base_score: Dict[str, float], weight: float) -> Dict[str, float]:
    return {k: round4(v * weight) for k, v in base_score.items()}


def calc_single_ingredient_function_scores(tag_detail: Dict[str, Any], weight: float) -> Dict[str, float]:
    """
    单独计算画像中展示的功能得分。
    这里只统计 fiber_functions 的直接规则贡献，不混入溶解性、发酵性、类别默认分。
    """
    result = {name: 0.0 for name in PORTRAIT_FUNCTION_SCORE_NAMES}

    for func in tag_detail.get("fiber_functions") or []:
        if func not in FUNCTION_SCORE_TARGETS:
            continue

        rule = FIBER_FUNCTION_RULES.get(func) or {}
        direct_score = sum(float(rule.get(score_key, 0.0)) for score_key in FUNCTION_SCORE_TARGETS[func])
        result[func] = result.get(func, 0.0) + direct_score * weight

    return {key: round4(value) for key, value in result.items()}


# =========================================================
# 7. 画像逻辑
# =========================================================
def get_level(value: float, low: float, high: float) -> str:
    if value >= high:
        return "高"
    elif value >= low:
        return "中"
    return "低"


def get_p_level(p_total: Optional[float]) -> Optional[str]:
    if p_total is None:
        return None
    normalized = min(100.0, max(0.0, p_total / FIBER_DISPLAY_SCALE_MAX * 100.0))
    if normalized < BUSINESS_LEVEL_THRESHOLDS["low_upper"]:
        return "低P"
    if normalized < BUSINESS_LEVEL_THRESHOLDS["high_lower"]:
        return "中P"
    return "高P"


def get_q_level(q_total: Optional[float]) -> Optional[str]:
    if q_total is None:
        return None
    normalized = min(100.0, max(0.0, q_total / FIBER_DISPLAY_SCALE_MAX * 100.0))
    if normalized < BUSINESS_LEVEL_THRESHOLDS["low_upper"]:
        return "低Q"
    if normalized < BUSINESS_LEVEL_THRESHOLDS["high_lower"]:
        return "中Q"
    return "高Q"


PQ_COMBO_MAPPING = {
    ("低P", "低Q"): "低纤低益生元基础型",
    ("低P", "中Q"): "轻修菌型",
    ("低P", "高Q"): "高益生元偏发酵型",
    ("中P", "低Q"): "温和托底型",
    ("中P", "中Q"): "平衡维护型",
    ("中P", "高Q"): "修菌主导型",
    ("高P", "低Q"): "高纤维纯托底型",
    ("高P", "中Q"): "高纤维温和修复型",
    ("高P", "高Q"): "高纤维高益生元协同型",
}


def get_pq_combo_name(p_level: Optional[str], q_level: Optional[str]) -> Optional[str]:
    if not p_level or not q_level:
        return None
    return PQ_COMBO_MAPPING.get((p_level, q_level))


def get_detail_display_name(detail: Dict[str, Any]) -> str:
    return (
        str(detail.get("matched_ingredient") or "").strip()
        or str(detail.get("tag_name") or "").strip()
    )


def get_role_score(detail: Dict[str, Any], role: str) -> float:
    if role == "p":
        return round4(
            get_detail_score(detail, "p_form")
            + get_detail_score(detail, "p_bulk")
            + get_detail_score(detail, "p_buffer")
        )
    if role == "q":
        return round4(get_detail_score(detail, "q_feed") + get_detail_score(detail, "q_scfa"))
    return 0.0


def format_main_ingredients(score_details: List[Dict[str, Any]], role: str) -> str:
    contributors = []
    for detail in score_details or []:
        score = get_role_score(detail, role)
        if score < MIN_COMPONENT_SCORE:
            continue
        name = get_detail_display_name(detail)
        if not name:
            continue
        contributors.append({
            "ingredient_name": name,
            "score": score,
            "rank": detail.get("rank"),
        })

    contributors.sort(key=lambda item: (-item["score"], item["rank"] or 9999))
    total_score = sum(item["score"] for item in contributors)
    if total_score <= 0:
        return ""

    selected = []
    cumulative = 0.0
    for item in contributors:
        if len(selected) >= MAX_MAIN_COMPONENT_COUNT:
            break
        selected.append(item)
        cumulative += item["score"]
        if cumulative >= total_score * MAIN_COMPONENT_CUMULATIVE_SHARE:
            break

    return "、".join(
        f"{item['ingredient_name']}({item['score']:.2f}, {item['score'] / total_score:.1%})"
        for item in selected
    )


def list_field(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def get_detail_score(detail: Dict[str, Any], score_key: str) -> float:
    weighted = detail.get("weighted_score") or {}
    return float(weighted.get(score_key) or 0.0)


def get_raw_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    raw_detail = detail.get("raw_tag_detail") or {}
    return raw_detail if isinstance(raw_detail, dict) else {}


def is_effective_detail(detail: Dict[str, Any]) -> bool:
    return float(detail.get("weight") or 0.0) > 0


def has_structural_bulk_driver(score_details: List[Dict[str, Any]]) -> bool:
    """
    骨架托底只在有明确结构型纤维证据时输出。
    果蔬类“混合纤维”虽然会贡献 p_bulk，但不直接命名为骨架托底。
    """
    for detail in score_details:
        if not is_effective_detail(detail) or get_detail_score(detail, "p_bulk") <= 0:
            continue

        tag_name = detail.get("tag_name")
        raw_detail = get_raw_detail(detail)
        fiber_functions = list_field(raw_detail.get("fiber_functions"))

        if "增加粪便骨架" in fiber_functions:
            return True
        if raw_detail.get("fiber_solubility") == "不可溶":
            return True
        if tag_name in STRUCTURAL_BULK_TAGS:
            return True

    return False


def has_explicit_microbiome_driver(detail: Dict[str, Any]) -> bool:
    if not is_effective_detail(detail):
        return False

    raw_detail = get_raw_detail(detail)
    ingredient_category = raw_detail.get("ingredient_category")
    prebiotic_functions = list_field(raw_detail.get("prebiotic_functions"))

    return bool(
        ingredient_category in {"益生元", "益生菌"}
        or prebiotic_functions
        or raw_detail.get("fermentability") == "高"
    )


def classify_microbiome_shape(q_feed: float, q_scfa: float, score_details: List[Dict[str, Any]]) -> str:
    if q_feed == 0 and q_scfa == 0:
        return "菌群调节不突出"

    explicit_drivers = [
        detail for detail in score_details
        if has_explicit_microbiome_driver(detail)
        and (get_detail_score(detail, "q_feed") + get_detail_score(detail, "q_scfa")) > 0
    ]

    if q_scfa > q_feed * 1.1:
        return "SCFA支持更明显"

    if explicit_drivers:
        categories = [
            get_raw_detail(detail).get("ingredient_category")
            for detail in explicit_drivers
        ]
        if "益生菌" in categories:
            return "益生菌支持驱动"
        if "益生元" in categories:
            return "益生元供菌驱动"
        return "高发酵底物驱动"

    return "温和供菌底物为主"


def get_g_contributors(score_details: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    contributors = []
    for detail in score_details:
        if not is_effective_detail(detail):
            continue

        g_score = get_detail_score(detail, "g")
        if g_score <= 0:
            continue

        raw_detail = get_raw_detail(detail)
        contributors.append({
            "tag_name": detail.get("tag_name"),
            "g_score": g_score,
            "ingredient_category": raw_detail.get("ingredient_category"),
            "fermentability": raw_detail.get("fermentability"),
            "prebiotic_functions": list_field(raw_detail.get("prebiotic_functions")),
        })

    contributors.sort(key=lambda item: item["g_score"], reverse=True)
    return contributors


def has_strong_fermentation_driver(contributors: List[Dict[str, Any]]) -> bool:
    for item in contributors:
        if (
            item["ingredient_category"] in {"益生元", "益生菌"}
            or item["prebiotic_functions"]
            or item["fermentability"] == "高"
        ):
            return True
    return False


def format_main_sources(contributors: List[Dict[str, Any]]) -> str:
    if not contributors:
        return ""

    total_g = sum(item["g_score"] for item in contributors)
    selected = []
    selected_g = 0.0
    top_g = contributors[0]["g_score"]

    for item in contributors:
        if len(selected) >= 3:
            break
        if selected and item["g_score"] < top_g * 0.5 and selected_g >= total_g * 0.6:
            break
        selected.append(str(item["tag_name"]))
        selected_g += item["g_score"]
        if selected_g >= total_g * 0.7:
            break

    return "、".join(selected)


def classify_fermentation_risk(g: float, score_details: List[Dict[str, Any]]) -> str:
    contributors = get_g_contributors(score_details)
    source_text = format_main_sources(contributors)
    source_suffix = f"（主要来自{source_text}）" if source_text else ""

    if g >= 2.5:
        return f"发酵推动感偏强{source_suffix}"

    if g >= 1.2:
        if has_strong_fermentation_driver(contributors):
            return f"有一定发酵推动感{source_suffix}"
        return f"温和发酵底物较多{source_suffix}"

    return "整体偏温和"


def format_function_score_summary(function_scores: Dict[str, float]) -> str:
    if not function_scores:
        return ""

    scores = {
        name: round4(float(function_scores.get(name) or 0.0))
        for name in PORTRAIT_FUNCTION_SCORE_NAMES
    }
    if not any(value > 0 for value in scores.values()):
        return ""

    score_text = "、".join(f"{name}{scores[name]:.2f}" for name in PORTRAIT_FUNCTION_SCORE_NAMES)
    return f"功能得分（{score_text}）"


def build_portrait(
    p_form: float,
    p_bulk: float,
    p_buffer: float,
    q_feed: float,
    q_scfa: float,
    g: float,
    score_details: Optional[List[Dict[str, Any]]] = None,
    function_scores: Optional[Dict[str, float]] = None,
) -> str:
    score_details = score_details or []
    p_total = p_form + p_bulk + p_buffer
    q_total = q_feed + q_scfa

    # 物理主导机制
    p_max = max(p_form, p_bulk, p_buffer)
    if p_max == 0:
        p_shape = "物理调节不突出"
    else:
        p_tags = []
        if p_bulk == p_max:
            if has_structural_bulk_driver(score_details):
                p_tags.append("偏骨架托底")
            else:
                p_tags.append("偏温和复合纤维支持")
        if p_form == p_max:
            p_tags.append("偏吸水成形")
        if p_buffer == p_max:
            p_tags.append("偏缓冲稀释")
        p_shape = "、".join(p_tags)

    # 菌群主导机制
    q_shape = classify_microbiome_shape(q_feed, q_scfa, score_details)

    # 总体结构
    if p_total == 0 and q_total == 0:
        overall = "整体调节特征不明显"
    elif p_total >= q_total * 1.2:
        overall = "整体偏物理托底型"
    elif q_total >= p_total * 1.2:
        overall = "整体偏菌群调节型"
    else:
        overall = "整体偏复合调节型"

    # 风险感
    risk = classify_fermentation_risk(g, score_details)

    # 强度描述
    p_level = (get_p_level(p_total) or "低P").replace("P", "")
    q_level = (get_q_level(q_total) or "低Q").replace("Q", "")

    portrait = f"{overall}；物理调节{p_level}（{p_shape}）；菌群调节{q_level}（{q_shape}）；{risk}"
    function_score_summary = format_function_score_summary(function_scores or {})
    if function_score_summary:
        portrait = f"{portrait}；{function_score_summary}"
    return portrait


# =========================================================
# 8. 核心计算
# =========================================================
def compute_scores(raw_ingredient_text: str, ingredient_feature_json: Dict[str, Any]) -> Dict[str, Any]:
    ingredient_list = split_ingredients(raw_ingredient_text)
    tag_detail_map = ingredient_feature_json.get("ingredient_tag_detail") or {}
    starch_burden_score, starch_ingredients = calc_starch_burden(ingredient_list)

    total = {
        "p_form": 0.0,
        "p_bulk": 0.0,
        "p_buffer": 0.0,
        "q_feed": 0.0,
        "q_scfa": 0.0,
        "g": 0.0,
    }

    matched_ingredients = []
    score_details = []
    function_score_total = {name: 0.0 for name in PORTRAIT_FUNCTION_SCORE_NAMES}

    for tag_name, detail in tag_detail_map.items():
        matched_ing, rank = find_best_match_rank(tag_name, ingredient_list)
        if should_skip_score_match(tag_name, matched_ing):
            continue
        weight = get_rank_weight(rank)

        base_score = calc_single_ingredient_base_score(detail)
        final_score = weighted_score(base_score, weight)
        function_score = calc_single_ingredient_function_scores(detail, weight)

        for k in total.keys():
            total[k] += final_score.get(k, 0.0)
        for func_name, score in function_score.items():
            function_score_total[func_name] += score

        matched_ingredients.append({
            "tag_name": tag_name,
            "matched_ingredient": matched_ing,
            "rank": rank,
            "weight": weight,
        })

        score_details.append({
            "tag_name": tag_name,
            "matched_ingredient": matched_ing,
            "rank": rank,
            "weight": weight,
            "base_score": base_score,
            "weighted_score": final_score,
            "function_score": function_score,
            "raw_tag_detail": detail,
        })

    p_form = round4(total["p_form"])
    p_bulk = round4(total["p_bulk"])
    p_buffer = round4(total["p_buffer"])
    q_feed = round4(total["q_feed"])
    q_scfa = round4(total["q_scfa"])
    g = round4(total["g"])

    p_total = round4(p_form + p_bulk + p_buffer)
    q_total = round4(q_feed + q_scfa)
    q_net = round4(q_total - g)
    pq_ratio = round4(p_total / q_total) if q_total > 0 else None

    portrait = build_portrait(
        p_form=p_form,
        p_bulk=p_bulk,
        p_buffer=p_buffer,
        q_feed=q_feed,
        q_scfa=q_scfa,
        g=g,
        score_details=score_details,
        function_scores={key: round4(value) for key, value in function_score_total.items()},
    )

    return {
        "p_form_score": p_form,
        "p_bulk_score": p_bulk,
        "p_buffer_score": p_buffer,
        "p_total_score": p_total,
        "q_feed_score": q_feed,
        "q_scfa_score": q_scfa,
        "q_total_score": q_total,
        "g_score": g,
        "q_net_score": q_net,
        "pq_ratio": pq_ratio,
        "starch_burden_score": starch_burden_score,
        "matched_ingredients_json": matched_ingredients,
        "score_detail_json": score_details,
        "starch_ingredients_json": starch_ingredients,
        "fiber_portrait": portrait,
    }


# =========================================================
# 9. 写入 SQL / 表结构维护
# =========================================================
INSERT_TARGET_SQL = f"""
INSERT INTO {TARGET_TABLE} (
    formula_id,
    source_id,
    product_key,
    brand,
    product_name,
    starch_burden_score,
    p_form_score,
    p_bulk_score,
    p_buffer_score,
    p_total_score,
    q_feed_score,
    q_scfa_score,
    q_total_score,
    p_level,
    q_level,
    pq_combo_name,
    p_main_ingredients,
    q_main_ingredients
) VALUES (
    %(formula_id)s,
    %(source_id)s,
    %(product_key)s,
    %(brand)s,
    %(product_name)s,
    %(starch_burden_score)s,
    %(p_form_score)s,
    %(p_bulk_score)s,
    %(p_buffer_score)s,
    %(p_total_score)s,
    %(q_feed_score)s,
    %(q_scfa_score)s,
    %(q_total_score)s,
    %(p_level)s,
    %(q_level)s,
    %(pq_combo_name)s,
    %(p_main_ingredients)s,
    %(q_main_ingredients)s
)
ON DUPLICATE KEY UPDATE
    formula_id = VALUES(formula_id),
    source_id = VALUES(source_id),
    brand = VALUES(brand),
    product_name = VALUES(product_name),
    starch_burden_score = VALUES(starch_burden_score),
    p_form_score = VALUES(p_form_score),
    p_bulk_score = VALUES(p_bulk_score),
    p_buffer_score = VALUES(p_buffer_score),
    p_total_score = VALUES(p_total_score),
    q_feed_score = VALUES(q_feed_score),
    q_scfa_score = VALUES(q_scfa_score),
    q_total_score = VALUES(q_total_score),
    p_level = VALUES(p_level),
    q_level = VALUES(q_level),
    pq_combo_name = VALUES(pq_combo_name),
    p_main_ingredients = VALUES(p_main_ingredients),
    q_main_ingredients = VALUES(q_main_ingredients);
"""


SOURCE_SCHEMA_COLUMNS = {
    "starch_ingredients_json": f"ALTER TABLE {SOURCE_TABLE} ADD COLUMN starch_ingredients_json JSON NULL AFTER ingredient_feature_json",
}


def table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (DB_CONFIG["database"], table_name),
    )
    return int(cursor.fetchone()["table_count"]) > 0


def ensure_source_table_schema(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """,
            (DB_CONFIG["database"], SOURCE_TABLE),
        )
        existing_columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}

        for column_name, alter_sql in SOURCE_SCHEMA_COLUMNS.items():
            if column_name not in existing_columns:
                cursor.execute(alter_sql)

def rebuild_target_table(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(CREATE_TARGET_TABLE_SQL)


def load_editable_score_config(conn) -> None:
    """Overlay code defaults with active database configuration."""
    global POSITION_WEIGHT_RULES, UNKNOWN_POSITION_WEIGHT
    with conn.cursor() as cursor:
        cursor.execute(f"""SELECT dimension_code,enum_value,score_value
          FROM {STANDARD_DB}.catfood_score_enum_config
          WHERE active=1 AND config_version='v1' AND domain_code IN ('fiber','carb')
            AND score_value IS NOT NULL""")
        rows = cursor.fetchall()
        cursor.execute(f"""SELECT rank_start,rank_end,position_weight,is_unknown
          FROM {STANDARD_DB}.catfood_score_position_weight_config
          WHERE active=1 AND config_version='v1' AND domain_code='global'
          ORDER BY is_unknown,rank_start""")
        position_rows = cursor.fetchall()
    by_dimension = {}
    for row in rows:
        by_dimension.setdefault(row["dimension_code"], {})[row["enum_value"]] = float(row["score_value"])
    for label in FERMENTABILITY_LEVELS:
        if label in by_dimension.get("fermentability_q_feed", {}):
            FERMENTABILITY_RULES[label]["q_feed"] = by_dimension["fermentability_q_feed"][label]
        if label in by_dimension.get("fermentability_q_scfa", {}):
            FERMENTABILITY_RULES[label]["q_scfa"] = by_dimension["fermentability_q_scfa"][label]
    for rule in STARCH_BASE_RULES:
        configured = by_dimension.get("starch_category", {}).get(rule["category"])
        if configured is not None:
            STARCH_LEVEL_SCORES[int(rule["level"])] = configured
    configured_ranges = []
    for row in position_rows:
        if row["is_unknown"]:
            UNKNOWN_POSITION_WEIGHT = float(row["position_weight"])
        else:
            configured_ranges.append((int(row["rank_start"]), int(row["rank_end"]), float(row["position_weight"])))
    if configured_ranges:
        POSITION_WEIGHT_RULES = configured_ranges


# =========================================================
# 10. 主流程
# =========================================================
def batch_process():
    conn = pymysql.connect(**DB_CONFIG)

    try:
        load_editable_score_config(conn)
        ensure_source_table_schema(conn)
        rebuild_target_table(conn)
        conn.commit()

        with conn.cursor() as cursor:
            formula_id = int(os.environ["FORMULA_ID"]) if os.getenv("FORMULA_ID") else None
            cursor.execute(f"""
                SELECT
                    s.formula_id,
                    b.standard_brand_name AS brand,
                    p.standard_product_name AS product_name,
                    s.raw_ingredient_text,
                    s.ingredient_feature_json
                FROM {SOURCE_TABLE} s
                JOIN {STANDARD_DB}.catfood_standard_formula f ON f.formula_id=s.formula_id
                JOIN {STANDARD_DB}.catfood_standard_product p ON p.product_id=f.product_id
                JOIN {STANDARD_DB}.catfood_standard_brand b ON b.brand_id=p.brand_id
                JOIN {STANDARD_DB}.catfood_formula_feature_profile gate
                  ON gate.formula_id=s.formula_id
                 AND gate.overall_status='ready_for_rebuild'
                WHERE (%s IS NULL OR s.formula_id=%s)
            """, (formula_id, formula_id))
            rows = cursor.fetchall()

        total_count = 0
        success_count = 0
        error_count = 0

        with conn.cursor() as cursor:
            for row in rows:
                total_count += 1

                source_id = row.get("formula_id")
                brand = row.get("brand")
                product_name = row.get("product_name")
                product_key = build_product_key(brand, product_name)
                raw_ingredient_text = row.get("raw_ingredient_text") or ""
                ingredient_feature_json = normalize_json_field(row.get("ingredient_feature_json"))

                try:
                    result = compute_scores(
                        raw_ingredient_text=raw_ingredient_text,
                        ingredient_feature_json=ingredient_feature_json,
                    )
                    p_level = get_p_level(result["p_total_score"])
                    q_level = get_q_level(result["q_total_score"])

                    params = {
                        "formula_id": row.get("formula_id"),
                        "source_id": source_id,
                        "product_key": product_key,
                        "brand": brand,
                        "product_name": product_name,
                        "p_form_score": result["p_form_score"],
                        "p_bulk_score": result["p_bulk_score"],
                        "p_buffer_score": result["p_buffer_score"],
                        "p_total_score": result["p_total_score"],
                        "q_feed_score": result["q_feed_score"],
                        "q_scfa_score": result["q_scfa_score"],
                        "q_total_score": result["q_total_score"],
                        "starch_burden_score": result["starch_burden_score"],
                        "p_level": p_level,
                        "q_level": q_level,
                        "pq_combo_name": get_pq_combo_name(p_level, q_level),
                        "p_main_ingredients": format_main_ingredients(result["score_detail_json"], "p"),
                        "q_main_ingredients": format_main_ingredients(result["score_detail_json"], "q"),
                    }
                    cursor.execute(INSERT_TARGET_SQL, params)
                    success_count += 1

                except Exception as e:
                    error_count += 1
                    print(f"[ERROR] source_id={source_id}, brand={brand}, product_name={product_name}, error={e}")

        conn.commit()

        print("处理完成")
        print(f"总数: {total_count}")
        print(f"成功: {success_count}")
        print(f"失败: {error_count}")

    finally:
        conn.close()


# =========================================================
# 11. 启动
# =========================================================
if __name__ == "__main__":
    batch_process()
