import json
import os
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Set, Tuple

import pymysql

from brand_normalizer import correct_brand


# =========================
# 1. 数据库配置
# =========================
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
    "cursorclass": pymysql.cursors.DictCursor,
}

SOURCE_DB = "csv_labeling"
SOURCE_TABLE = "catfood_ingredient_ocr_parsed"

TARGET_DB = "protein_feature_platform"
TARGET_TABLE = "catfood_fiber_feature_json"


# =========================
# 2. 原料规则词典
# 标准名 -> 标签属性
# =========================
INGREDIENT_RULES: Dict[str, Dict] = {
    # -------------------------
    # 膳食纤维：经典纤维
    # -------------------------
    "车前子壳": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "可溶",
        "fermentability": "中低",
        "fiber_functions": ["吸水成形", "延缓通过", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "纤维素": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },
    "竹纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },

    # -------------------------
    # 膳食纤维：豆类纤维源
    # -------------------------
    "豌豆纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },
    "豆纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },
    "豌豆壳纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },
    "大豆纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },
    "鹰嘴豆纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },

    # -------------------------
    # 膳食纤维：果蔬纤维源
    # -------------------------
    "南瓜": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "冬南瓜": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "苹果果肉": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "苹果纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "柑橘纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "缓冲刺激", "温和供菌底物"],
        "prebiotic_functions": [],
    },
    "甜菜纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "增加粪便骨架", "温和供菌底物"],
        "prebiotic_functions": [],
    },
    "甜菜粕": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "增加粪便骨架", "温和供菌底物"],
        "prebiotic_functions": [],
    },
    "胡萝卜": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "温和供菌底物"],
        "prebiotic_functions": [],
    },
    "胡萝卜粉": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "温和供菌底物"],
        "prebiotic_functions": [],
    },
    "西葫芦": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "番茄纤维": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "菠菜": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中低",
        "fiber_functions": ["温和供菌底物"],
        "prebiotic_functions": [],
    },
    "甘蓝": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中低",
        "fiber_functions": ["温和供菌底物"],
        "prebiotic_functions": [],
    },
    "芜菁叶": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中低",
        "fiber_functions": ["温和供菌底物"],
        "prebiotic_functions": [],
    },
    "甜菜叶": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中低",
        "fiber_functions": ["温和供菌底物"],
        "prebiotic_functions": [],
    },
    "蔓越莓": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中低",
        "fiber_functions": ["温和供菌底物"],
        "prebiotic_functions": [],
    },
    "蓝莓": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中低",
        "fiber_functions": ["温和供菌底物"],
        "prebiotic_functions": [],
    },
    "梨": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["吸水成形", "温和供菌底物"],
        "prebiotic_functions": [],
    },

    # -------------------------
    # 膳食纤维：藻类纤维源
    # -------------------------
    "海带干": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "海带": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "海藻": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "海藻粉": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中",
        "fiber_functions": ["温和供菌底物", "缓冲刺激"],
        "prebiotic_functions": [],
    },
    "螺旋藻": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "混合",
        "fermentability": "中低",
        "fiber_functions": ["温和供菌底物"],
        "prebiotic_functions": [],
    },

    # -------------------------
    # 膳食纤维：草本/植物结构纤维
    # -------------------------
    "苜蓿草颗粒": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },
    "苜蓿草": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },
    "苜蓿粉": {
        "ingredient_category": "膳食纤维",
        "fiber_solubility": "不可溶",
        "fermentability": "低",
        "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
        "prebiotic_functions": [],
    },

    # -------------------------
    # 功能性补丁：酵母 / 葡聚糖 / 甜菜浆 / 果寡糖
    # -------------------------
    "啤酒酵母粉": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "中低",
        "fiber_functions": [],
        "prebiotic_functions": ["生态支持", "屏障支持辅助"],
    },
    "β-葡聚糖": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "中低",
        "fiber_functions": [],
        "prebiotic_functions": ["屏障支持", "生态支持", "免疫调节辅助"],
    },

    # -------------------------
    # 益生元
    # -------------------------
    "菊粉": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "高",
        "fiber_functions": [],
        "prebiotic_functions": ["供菌", "SCFA支持", "促进有益菌增殖"],
    },
    "菊苣菊糖": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "高",
        "fiber_functions": [],
        "prebiotic_functions": ["供菌", "SCFA支持", "促进有益菌增殖"],
    },
    "FOS": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "高",
        "fiber_functions": [],
        "prebiotic_functions": ["供菌", "SCFA支持", "促进有益菌增殖"],
    },
    "低聚果糖": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "高",
        "fiber_functions": [],
        "prebiotic_functions": ["供菌", "SCFA支持", "促进有益菌增殖"],
    },
    "MOS": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "中",
        "fiber_functions": [],
        "prebiotic_functions": ["黏附竞争", "屏障支持", "生态支持"],
    },
    "甘露寡糖": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "中",
        "fiber_functions": [],
        "prebiotic_functions": ["黏附竞争", "屏障支持", "生态支持"],
    },
    "酵母细胞壁": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "中",
        "fiber_functions": [],
        "prebiotic_functions": ["黏附竞争", "屏障支持", "生态支持"],
    },
    "GOS": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "高",
        "fiber_functions": [],
        "prebiotic_functions": ["供菌", "SCFA支持", "促进有益菌增殖"],
    },
    "低聚半乳糖": {
        "ingredient_category": "益生元",
        "fiber_solubility": None,
        "fermentability": "高",
        "fiber_functions": [],
        "prebiotic_functions": ["供菌", "SCFA支持", "促进有益菌增殖"],
    },
}


# =========================
# 3. OCR / 别名归一化
# pattern -> standard_name
# =========================
NORMALIZATION_PATTERNS: List[Tuple[str, str]] = [
    # OCR错字
    (r"苜宿草颗粒", "苜蓿草颗粒"),
    (r"苜宿草", "苜蓿草"),
    (r"裂壶漠粉", "裂壶藻粉"),

    # 新补丁
    (r"脱水甜菜浆", "甜菜粕"),
    (r"甜菜浆", "甜菜粕"),
    (r"果寡糖", "FOS"),
    (r"β-?1,?3-?D-?葡聚糖", "β-葡聚糖"),
    (r"β-?葡聚糖", "β-葡聚糖"),
    (r"β葡聚糖", "β-葡聚糖"),
    (r"啤酒酵母粉", "啤酒酵母粉"),

    # 常见简写/近义
    (r"车前子$", "车前子壳"),
    (r"车前子壳", "车前子壳"),
    (r"洋车前子壳", "车前子壳"),
    (r"豌豆纤维", "豌豆纤维"),
    (r"豌豆壳纤维", "豌豆壳纤维"),
    (r"大豆纤维", "大豆纤维"),
    (r"豆纤维", "豆纤维"),
    (r"鹰嘴豆纤维", "鹰嘴豆纤维"),
    (r"苹果果肉", "苹果果肉"),
    (r"苹果纤维", "苹果纤维"),
    (r"苹果梨", "苹果果肉"),
    (r"南瓜籽", "南瓜籽"),
    (r"南瓜", "南瓜"),
    (r"冬南瓜", "冬南瓜"),
    (r"西葫芦", "西葫芦"),
    (r"胡萝卜粉", "胡萝卜粉"),
    (r"胡萝卜", "胡萝卜"),
    (r"干制胡萝卜丁西葫芦", "胡萝卜"),
    (r"干制胡萝卜丁", "胡萝卜"),
    (r"柑橘纤维", "柑橘纤维"),
    (r"甜菜粕", "甜菜粕"),
    (r"甜菜纤维", "甜菜纤维"),
    (r"菠菜", "菠菜"),
    (r"甘蓝", "甘蓝"),
    (r"芜菁叶", "芜菁叶"),
    (r"甜菜叶", "甜菜叶"),
    (r"蔓越莓", "蔓越莓"),
    (r"蓝莓", "蓝莓"),
    (r"海带干", "海带干"),
    (r"海带", "海带"),
    (r"海藻粉", "海藻粉"),
    (r"海藻", "海藻"),
    (r"螺旋藻", "螺旋藻"),
    (r"竹纤维", "竹纤维"),
    (r"纤维素", "纤维素"),
    (r"苜蓿草颗粒", "苜蓿草颗粒"),
    (r"苜蓿草", "苜蓿草"),
    (r"苜蓿粉", "苜蓿粉"),
    (r"菊苣菊糖", "菊苣菊糖"),
    (r"菊粉", "菊粉"),
    (r"菊苣根", "菊粉"),
    (r"低聚果糖", "低聚果糖"),
    (r"甘露寡糖", "甘露寡糖"),
    (r"酵母细胞壁", "酵母细胞壁"),
    (r"低聚半乳糖", "低聚半乳糖"),
    (r"\bMOS\b", "MOS"),
    (r"\bFOS\b", "FOS"),
    (r"\bGOS\b", "GOS"),
]


# =========================
# 4. 广义关键词辅助识别
# 用于没命中精确词典时兜底
# =========================
GENERIC_KEYWORDS: List[Tuple[str, Dict]] = [
    # 豆类纤维源
    (
        r"(豌豆纤维|豆纤维|豆壳纤维|豌豆壳纤维|大豆纤维|鹰嘴豆纤维)",
        {
            "ingredient_category": "膳食纤维",
            "fiber_solubility": "不可溶",
            "fermentability": "低",
            "fiber_functions": ["增加粪便骨架", "稀释刺激物"],
            "prebiotic_functions": [],
        }
    ),
    # 果蔬纤维源
    (
        r"(南瓜(?!籽)|冬南瓜|苹果果肉|苹果纤维|胡萝卜|胡萝卜粉|西葫芦|柑橘纤维|甜菜粕|甜菜纤维|菠菜|甘蓝|芜菁叶|甜菜叶|蔓越莓|蓝莓|梨)",
        {
            "ingredient_category": "膳食纤维",
            "fiber_solubility": "混合",
            "fermentability": "中",
            "fiber_functions": ["温和供菌底物"],
            "prebiotic_functions": [],
        }
    ),
    # 藻类纤维源
    (
        r"(海带|海带干|海藻|海藻粉|螺旋藻)",
        {
            "ingredient_category": "膳食纤维",
            "fiber_solubility": "混合",
            "fermentability": "中",
            "fiber_functions": ["温和供菌底物", "缓冲刺激"],
            "prebiotic_functions": [],
        }
    ),
    # 新补丁：甜菜浆
    (
        r"(甜菜浆|脱水甜菜浆)",
        {
            "ingredient_category": "膳食纤维",
            "fiber_solubility": "混合",
            "fermentability": "中",
            "fiber_functions": ["吸水成形", "增加粪便骨架", "温和供菌底物"],
            "prebiotic_functions": [],
        }
    ),
    # 新补丁：果寡糖 / FOS
    (
        r"(果寡糖|FOS)",
        {
            "ingredient_category": "益生元",
            "fiber_solubility": None,
            "fermentability": "高",
            "fiber_functions": [],
            "prebiotic_functions": ["供菌", "SCFA支持", "促进有益菌增殖"],
        }
    ),
    # 新补丁：β-葡聚糖
    (
        r"(β-?1,?3-?D-?葡聚糖|β-?葡聚糖|β葡聚糖)",
        {
            "ingredient_category": "益生元",
            "fiber_solubility": None,
            "fermentability": "中低",
            "fiber_functions": [],
            "prebiotic_functions": ["屏障支持", "生态支持", "免疫调节辅助"],
        }
    ),
    # 新补丁：啤酒酵母粉
    (
        r"(啤酒酵母粉)",
        {
            "ingredient_category": "益生元",
            "fiber_solubility": None,
            "fermentability": "中低",
            "fiber_functions": [],
            "prebiotic_functions": ["生态支持", "屏障支持辅助"],
        }
    ),
]


# =========================
# 5. 文本清洗
# =========================
def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = text.strip()
    text = text.replace("，", ",").replace("；", ",").replace("、", ",")
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\s+", "", text)

    # 去掉百分比说明
    text = re.sub(r"\([^)]*?\d+(?:\.\d+)?%\)", "", text)
    # 去掉来源说明
    text = re.sub(r"\((来源于[^)]*?)\)", "", text)
    # 去掉天然风味剂说明
    text = re.sub(r"\((天然风味剂)\)", "", text)

    return text


def split_ingredients(text: str) -> List[str]:
    if not text:
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


def build_product_key(brand: str, product_name: str) -> str:
    return f"{(brand or '').strip()}||{(product_name or '').strip()}"


STARCH_EXCLUDE_KEYWORDS = [
    "纤维", "蛋白", "油", "提取物", "果寡糖", "低聚果糖", "菊粉", "菊糖",
    "酵母", "维生素", "矿物质", "益生菌",
]

STARCH_BASE_RULES = [
    {
        "category": "精制淀粉/纯淀粉",
        "base_score": 2.0,
        "keywords": [
            "玉米淀粉", "小麦淀粉", "马铃薯淀粉", "木薯淀粉",
            "豌豆淀粉", "变性淀粉", "淀粉",
        ],
    },
    {
        "category": "高淀粉粉类",
        "base_score": 1.8,
        "keywords": [
            "木薯粉", "马铃薯粉", "土豆粉", "玉米粉",
            "小麦粉", "大米粉", "米粉",
        ],
    },
    {
        "category": "薯类淀粉来源",
        "base_score": 1.5,
        "keywords": [
            "木薯", "马铃薯", "土豆", "红薯", "甘薯", "紫薯", "地瓜",
        ],
    },
    {
        "category": "谷物淀粉来源",
        "base_score": 1.3,
        "keywords": [
            "碎米", "大米", "白米", "糙米", "酿酒米", "酿造米", "燕麦", "小麦", "玉米", "高粱",
            "大麦", "小米", "藜麦",
        ],
    },
    {
        "category": "豆类碳水来源",
        "base_score": 1.2,
        "keywords": [
            "豌豆", "鹰嘴豆", "扁豆", "绿豆", "蚕豆",
        ],
    },
]


def round4(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def rank_weight(rank: int) -> float:
    if rank == 1:
        return 1.2
    if rank <= 3:
        return 1.0
    if rank <= 5:
        return 0.8
    if rank <= 8:
        return 0.6
    if rank <= 12:
        return 0.4
    return 0.2


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


def classify_starch_ingredient(ingredient: str) -> Optional[Dict]:
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
                "base_score": float(rule["base_score"]),
                "matched_keywords": matched_keywords,
            }

    return None


def calc_starch_ingredients(ingredient_list: List[str]) -> List[Dict]:
    details = []
    for rank, ingredient in enumerate(ingredient_list, start=1):
        starch_info = classify_starch_ingredient(ingredient)
        if not starch_info:
            continue

        weight = rank_weight(rank)
        weighted = round4(starch_info["base_score"] * weight)
        details.append({
            "ingredient_name": ingredient,
            "rank": rank,
            "weight": weight,
            "category": starch_info["category"],
            "matched_keywords": starch_info["matched_keywords"],
            "base_score": starch_info["base_score"],
            "weighted_score": weighted,
        })

    return details


def normalize_ingredient_name(name: str) -> str:
    raw = name.strip()

    # 去掉括号说明
    raw = re.sub(r"\([^)]*\)", "", raw)
    raw = re.sub(r"（[^）]*）", "", raw)
    raw = raw.strip()

    for pattern, replacement in NORMALIZATION_PATTERNS:
        if re.search(pattern, raw, flags=re.IGNORECASE):
            return replacement

    return raw


def match_tagged_ingredient(name: str) -> Optional[Tuple[str, Dict]]:
    """
    返回:
    (标准名, 规则dict)
    """
    if "南瓜籽" in name:
        return None

    # 1. 精确匹配
    if name in INGREDIENT_RULES:
        return name, INGREDIENT_RULES[name]

    # 2. 包含匹配：只允许原料文本包含完整标准名。
    # 不能反向使用 `name in std_name`，否则“豌豆”会被扩写成“豌豆纤维”。
    for std_name, rule in INGREDIENT_RULES.items():
        if std_name in name:
            return std_name, rule

    # 3. 通用关键词兜底
    for pattern, rule in GENERIC_KEYWORDS:
        if re.search(pattern, name, flags=re.IGNORECASE):
            return name, rule

    return None


def build_feature_json(matched_items: List[Tuple[str, Dict]]) -> Dict:
    ingredient_category_tags: Set[str] = set()
    ingredient_subtype_tags: Dict[str, List[str]] = defaultdict(list)
    ingredient_tag_detail: Dict[str, Dict] = {}

    for ingredient_name, rule in matched_items:
        category = rule["ingredient_category"]

        ingredient_category_tags.add(category)
        if ingredient_name not in ingredient_subtype_tags[category]:
            ingredient_subtype_tags[category].append(ingredient_name)

        ingredient_tag_detail[ingredient_name] = {
            "ingredient_category": rule["ingredient_category"],
            "fiber_solubility": rule["fiber_solubility"],
            "fermentability": rule["fermentability"],
            "fiber_functions": rule["fiber_functions"],
            "prebiotic_functions": rule["prebiotic_functions"],
        }

    return {
        "ingredient_category_tags": sorted(list(ingredient_category_tags)),
        "ingredient_subtype_tags": {
            category: sorted(subtypes)
            for category, subtypes in sorted(ingredient_subtype_tags.items())
        },
        "ingredient_tag_detail": ingredient_tag_detail,
    }


# =========================
# 6. 读取、聚合、写入
# =========================
def main() -> None:
    conn = pymysql.connect(**DB_CONFIG)

    try:
        read_sql = f"""
            SELECT s.id, s.brand, s.product_name, s.image_name, s.image_path, s.ingredient_composition
            FROM {SOURCE_DB}.{SOURCE_TABLE} s
            WHERE s.ingredient_composition IS NOT NULL
              AND s.ingredient_composition <> ''
              AND EXISTS (
                  SELECT 1
                  FROM {SOURCE_DB}.{SOURCE_TABLE} changed
                  LEFT JOIN {TARGET_DB}.{TARGET_TABLE} t
                    ON COALESCE(TRIM(t.brand), '') = COALESCE(TRIM(changed.brand), '')
                   AND COALESCE(TRIM(t.product_name), '') = COALESCE(TRIM(changed.product_name), '')
                  WHERE changed.ingredient_composition IS NOT NULL
                    AND changed.ingredient_composition <> ''
                    AND COALESCE(TRIM(changed.brand), '') = COALESCE(TRIM(s.brand), '')
                    AND COALESCE(TRIM(changed.product_name), '') = COALESCE(TRIM(s.product_name), '')
                    AND (
                        t.id IS NULL
                        OR t.updated_at IS NULL
                        OR changed.updated_ts > t.updated_at
                    )
              )
        """

        create_table_sql = f"""
            CREATE TABLE IF NOT EXISTS {TARGET_DB}.{TARGET_TABLE} (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                product_key VARCHAR(600) NULL,
                brand VARCHAR(255) NOT NULL,
                product_name VARCHAR(255) NOT NULL,
                source_ids JSON,
                raw_ingredient_text LONGTEXT,
                ingredient_feature_json JSON NOT NULL,
                starch_ingredients_json JSON NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_product_key (product_key),
                UNIQUE KEY uniq_brand_product (brand, product_name)
            )
        """

        upsert_sql = f"""
            INSERT INTO {TARGET_DB}.{TARGET_TABLE}
                (
                    product_key, brand, product_name, source_ids,
                    raw_ingredient_text, ingredient_feature_json, starch_ingredients_json
                )
            VALUES
                (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                product_key = VALUES(product_key),
                source_ids = VALUES(source_ids),
                raw_ingredient_text = VALUES(raw_ingredient_text),
                ingredient_feature_json = VALUES(ingredient_feature_json),
                starch_ingredients_json = VALUES(starch_ingredients_json),
                updated_at = CURRENT_TIMESTAMP
        """

        with conn.cursor() as cursor:
            cursor.execute(create_table_sql)
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
                  AND COLUMN_NAME = 'product_key'
                """,
                (TARGET_DB, TARGET_TABLE),
            )
            if cursor.fetchone() is None:
                cursor.execute(f"ALTER TABLE {TARGET_DB}.{TARGET_TABLE} ADD COLUMN product_key VARCHAR(600) NULL AFTER id")
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
                  AND COLUMN_NAME = 'starch_ingredients_json'
                """,
                (TARGET_DB, TARGET_TABLE),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    f"ALTER TABLE {TARGET_DB}.{TARGET_TABLE} "
                    "ADD COLUMN starch_ingredients_json JSON NULL AFTER ingredient_feature_json"
                )
            cursor.execute(
                """
                SELECT COUNT(*) AS index_count
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
                  AND INDEX_NAME = 'idx_product_key'
                """,
                (TARGET_DB, TARGET_TABLE),
            )
            if int(cursor.fetchone()["index_count"]) == 0:
                cursor.execute(f"ALTER TABLE {TARGET_DB}.{TARGET_TABLE} ADD KEY idx_product_key (product_key)")

        with conn.cursor() as cursor:
            cursor.execute(read_sql)
            rows = cursor.fetchall()

        grouped = defaultdict(lambda: {
            "ids": [],
            "ingredient_texts": [],
        })

        for row in rows:
            brand = correct_brand(
                (row["brand"] or "").strip(),
                (row["product_name"] or "").strip(),
                row.get("image_name"),
                row.get("image_path"),
            )
            product_name = (row["product_name"] or "").strip()
            key = (brand, product_name)

            grouped[key]["ids"].append(row["id"])
            grouped[key]["ingredient_texts"].append(row["ingredient_composition"])

        processed_count = 0

        with conn.cursor() as cursor:
            for (brand, product_name), payload in grouped.items():
                merged_text = "，".join(payload["ingredient_texts"])
                normalized_text = normalize_text(merged_text)
                raw_ingredients = split_ingredients(normalized_text)

                matched_items: List[Tuple[str, Dict]] = []
                seen: Set[str] = set()

                for raw_name in raw_ingredients:
                    normalized_name = normalize_ingredient_name(raw_name)
                    matched = match_tagged_ingredient(normalized_name)
                    if matched:
                        std_name, rule = matched
                        if std_name not in seen:
                            matched_items.append((std_name, rule))
                            seen.add(std_name)

                feature_json = build_feature_json(matched_items)
                starch_ingredients = calc_starch_ingredients(raw_ingredients)

                cursor.execute(
                    upsert_sql,
                    (
                        build_product_key(brand, product_name),
                        brand,
                        product_name,
                        json.dumps(payload["ids"], ensure_ascii=False),
                        merged_text,
                        json.dumps(feature_json, ensure_ascii=False),
                        json.dumps(starch_ingredients, ensure_ascii=False),
                    )
                )
                processed_count += 1

        conn.commit()
        print(f"处理完成，共写入/更新 {processed_count} 条 brand+product_name 数据。")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
