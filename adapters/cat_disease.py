# -*- coding: utf-8 -*-
"""
猫粮评论病症抽取与结构化标注脚本

功能：
1. 从本地 MySQL 评论表读取评论
2. 使用关键词做第一轮候选召回
3. 调用通义千问进行病症标准化结构标注
4. 将结果写入标准化结果表

输出结果表字段：
id
comment_hash
brand_name
review_text
symptom_category
symptom_name
effect_direction
review_date_raw
review_date
processed_at
confidence
evidence_text
model_name
prompt_version
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime
from threading import Lock
from typing import List, Dict, Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text
from openai import OpenAI

import app_config as runtime_config
from adapters.comment_data_adapter import load_default_db_config, load_settings

from vendor.csv_mysql_labeling.src.extract_catfood_brand_relations import (
    _find_brand_mentions,
    _ordered_unique_brands,
)


# =========================================================
# 1. MySQL 配置
# =========================================================

MYSQL_CONFIG = runtime_config.get_mysql_config()


# =========================================================
# 2. 表名配置
# =========================================================

# 你的原始评论表
SOURCE_TABLE = "raw_review_comment"

# 输出的病症结构化结果表
TARGET_TABLE = "review_symptom_event_result"

# API 默认输入表：评论健康候选表
DISEASE_SOURCE_TABLE = "catfood_brand_health_candidates"

# API 默认输出表：人工审核前的病症结构化候选表
DISEASE_REVIEW_TABLE = "cat_disease_clue_candidates"

GROUP_CONCAT_MAX_LEN = 1024 * 1024
DEFAULT_MAX_COMMENT_CHARS = 500
DEFAULT_LLM_TIMEOUT_SECONDS = int(os.getenv("DISEASE_STRUCTURE_LLM_TIMEOUT_SECONDS", "45"))
MAX_LLM_CALLS_PER_MINUTE = int(os.getenv("DISEASE_STRUCTURE_MAX_LLM_CALLS_PER_MINUTE", "30"))
_llm_rate_lock = Lock()
_last_llm_call_at = 0.0


# =========================================================
# 3. 原始评论表字段映射
# =========================================================
"""
把这里改成你本地评论表真实字段名。

例如你的原始表字段如果是：
brand_name, content, comment_time

那就写：
SOURCE_FIELD_MAP = {
    "source_id": "id",
    "brand_name": "brand_name",
    "review_text": "content",
    "review_date_raw": "comment_time",
    "review_date": "comment_time"
}
"""

SOURCE_FIELD_MAP = {
    "source_id": "id",
    "brand_name": "brand_name",
    "review_text": "review_text",
    "review_date_raw": "review_date_raw",
    "review_date": "review_date"
}


# =========================================================
# 4. 通义千问配置
# =========================================================

"""
安装依赖：
pip install openai sqlalchemy pymysql pandas

设置环境变量：

Mac / Linux:
export DASHSCOPE_API_KEY="你的通义千问API_KEY"

Windows PowerShell:
setx DASHSCOPE_API_KEY "你的通义千问API_KEY"
"""

_DEFAULT_QWEN_CONFIG = runtime_config.get_qwen_config()
LLM_MODEL = _DEFAULT_QWEN_CONFIG["model"]
LLM_BASE_URL = _DEFAULT_QWEN_CONFIG["base_url"]
LLM_API_KEY = _DEFAULT_QWEN_CONFIG["api_key"]

PROMPT_VERSION = "symptom_extract_v1"


# =========================================================
# 5. 关键词召回词典
# =========================================================

SYMPTOM_KEYWORDS = {
    "消化系统问题": {
        "软便/拉稀": [
            "软便", "便软", "拉稀", "腹泻", "拉肚子", "不成形",
            "稀便", "糊状便", "便便稀", "便便不成形", "反复拉",
            "便血", "肠胃不好", "肠胃不适", "肚子不舒服"
        ],
        "拉屎臭": [
            "屎臭", "拉屎臭", "便便臭", "臭便", "巨臭",
            "特别臭", "粑粑臭", "臭臭", "拉的臭"
        ],
        "呕吐": [
            "呕吐", "吐了", "吐粮", "吐黄水", "吐猫粮",
            "吃完吐", "反胃", "吐", "吐毛球"
        ],
        "便秘/干硬便": [
            "便秘", "拉不出来", "小疙瘩", "干便", "硬便",
            "一粒一粒", "不是一长条", "颗粒便", "羊屎蛋"
        ]
    },
    "皮肤毛发问题": {
        "黑下巴": [
            "黑下巴", "下巴黑", "毛囊炎", "粉刺", "油下巴",
            "下巴脏", "下巴结痂", "下巴有黑点"
        ],
        "油尾巴": [
            "油尾巴", "尾巴油", "种马尾"
        ],
        "掉毛": [
            "掉毛", "脱毛", "掉毛严重", "毛变少", "秃"
        ],
        "瘙痒/过敏": [
            "过敏", "瘙痒", "抓挠", "红疹", "皮肤红",
            "皮屑", "挠", "痒"
        ]
    },
    "眼部问题": {
        "泪痕": [
            "泪痕", "流泪", "眼泪多", "眼屎", "眼睛分泌物",
            "眼周发红", "眼泪"
        ]
    },
    "适口性问题": {
        "不爱吃": [
            "不爱吃", "不吃", "挑食", "闻了就走", "适口性差",
            "不肯吃", "吃得少"
        ]
    }
}


VALID_SYMPTOM_CATEGORIES = [
    "消化系统问题",
    "皮肤毛发问题",
    "眼部问题",
    "适口性问题",
    "其他健康问题"
]

VALID_SYMPTOM_NAMES = [
    "软便/拉稀",
    "拉屎臭",
    "呕吐",
    "便秘/干硬便",
    "黑下巴",
    "油尾巴",
    "掉毛",
    "瘙痒/过敏",
    "泪痕",
    "不爱吃",
    "其他"
]

VALID_EFFECT_DIRECTIONS = [
    "加重",
    "改善",
    "不确定"
]


# =========================================================
# 6. 数据库工具
# =========================================================

def get_mysql_engine():
    url = (
        f"mysql+pymysql://{MYSQL_CONFIG['user']}:{MYSQL_CONFIG['password']}"
        f"@{MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}"
        f"?charset={MYSQL_CONFIG['charset']}"
    )
    return create_engine(url, pool_pre_ping=True)


TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_table(name: str) -> str:
    if not TABLE_NAME_RE.fullmatch(name or ""):
        raise ValueError(f"Invalid table name: {name}")
    return name


def _db_config(payload_db: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        config = load_default_db_config()
    except Exception:
        config = dict(MYSQL_CONFIG)
    config.update(payload_db or {})
    config["port"] = int(config.get("port") or 3306)
    config["charset"] = str(config.get("charset") or "utf8mb4")
    return config


def _mysql_url(db_config: Dict[str, Any]) -> str:
    return (
        f"mysql+pymysql://{db_config['user']}:{db_config.get('password') or ''}"
        f"@{db_config['host']}:{db_config['port']}/{db_config['database']}"
        f"?charset={db_config['charset']}"
    )


def get_engine_from_config(payload_db: Optional[Dict[str, Any]] = None):
    return create_engine(_mysql_url(_db_config(payload_db)), pool_pre_ping=True, future=True)


def create_target_table(engine):
    sql = f"""
    CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        source_id VARCHAR(100),
        comment_hash VARCHAR(64) NOT NULL,
        brand_name VARCHAR(100) NOT NULL DEFAULT '',
        review_text TEXT,

        symptom_category VARCHAR(100),
        symptom_name VARCHAR(100),
        effect_direction VARCHAR(50),

        review_date_raw VARCHAR(100),
        review_date DATE,
        processed_at DATETIME DEFAULT CURRENT_TIMESTAMP,

        confidence DECIMAL(5,4),
        evidence_text TEXT,

        hit_keywords TEXT,
        model_name VARCHAR(100),
        prompt_version VARCHAR(50),

        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

        UNIQUE KEY uk_comment_symptom (
            comment_hash,
            symptom_category,
            symptom_name,
            effect_direction
        )
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    with engine.begin() as conn:
        conn.execute(text(sql))


# =========================================================
# 7. 读取原始评论
# =========================================================

def load_reviews(engine, limit: Optional[int] = None) -> pd.DataFrame:
    source_id_col = SOURCE_FIELD_MAP["source_id"]
    brand_col = SOURCE_FIELD_MAP["brand_name"]
    text_col = SOURCE_FIELD_MAP["review_text"]
    raw_date_col = SOURCE_FIELD_MAP["review_date_raw"]
    date_col = SOURCE_FIELD_MAP["review_date"]

    sql = f"""
    SELECT
        {source_id_col} AS source_id,
        {brand_col} AS brand_name,
        {text_col} AS review_text,
        {raw_date_col} AS review_date_raw,
        {date_col} AS review_date
    FROM {SOURCE_TABLE}
    WHERE {text_col} IS NOT NULL
      AND {text_col} <> ''
    """

    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    return pd.read_sql(sql, engine)


# =========================================================
# 8. 关键词召回
# =========================================================

def keyword_recall(review_text: str) -> List[Dict[str, Any]]:
    if not review_text:
        return []

    review_text = str(review_text)
    hits = []

    for category, symptom_map in SYMPTOM_KEYWORDS.items():
        for symptom_name, keywords in symptom_map.items():
            matched_keywords = []

            for kw in keywords:
                if kw in review_text:
                    matched_keywords.append(kw)

            if matched_keywords:
                hits.append({
                    "symptom_category": category,
                    "symptom_name": symptom_name,
                    "matched_keywords": matched_keywords
                })

    return hits


def is_candidate_review(review_text: str) -> bool:
    return len(keyword_recall(review_text)) > 0


def flatten_hit_keywords(keyword_hits: List[Dict[str, Any]]) -> str:
    words = []

    for hit in keyword_hits:
        words.extend(hit.get("matched_keywords", []))

    return ",".join(sorted(set(words)))


FALLBACK_BRAND_PATTERNS = {
    "皇家": ["皇家", "阿皇", "K36", "BK34", "BK36", "BS34", "F32"],
    "渴望": ["渴望", "orijen"],
    "爱肯拿": ["爱肯拿", "安肯拿", "acana"],
    "弗列加特": ["弗列加特"],
    "麦富迪": ["麦富迪", "barf"],
    "蓝氏": ["蓝氏"],
    "鲜朗": ["鲜朗", "鲜郎"],
    "素力高": ["素力高", "solid gold"],
    "福摩": ["绿福摩", "福摩", "fromm"],
    "百利": ["百利", "百丽", "instinct"],
    "巅峰": ["巅峰", "ziwi"],
    "冠能": ["冠能", "pro plan"],
    "网易严选": ["网易严选"],
    "自然光环": ["自然光环", "自然光", "halo"],
    "法米娜": ["法米娜"],
    "纽翠斯": ["纽翠斯"],
    "纽顿": ["纽顿"],
    "玫斯": ["玫斯"],
    "纯福": ["纯福"],
    "领先": ["领先"],
    "顽皮": ["顽皮"],
    "比瑞吉": ["比瑞吉"],
    "有鱼": ["有鱼"],
    "狂野盛宴": ["狂野盛宴"],
    "帕特": ["帕特"],
    "好主人": ["好主人"],
    "卫仕": ["卫仕", "卫士"],
    "GO!": ["GO", "go"],
    "NOW": ["NOW", "now"],
}


def extract_mentioned_brands(text_value: Any) -> List[str]:
    text_value = str(text_value or "")
    if not text_value.strip():
        return []

    if _find_brand_mentions is not None and _ordered_unique_brands is not None:
        return list(_ordered_unique_brands(_find_brand_mentions(text_value)))

    hits: List[tuple[int, str]] = []
    lower_text = text_value.lower()
    for canonical, aliases in FALLBACK_BRAND_PATTERNS.items():
        positions = [
            lower_text.find(alias.lower())
            for alias in aliases
            if alias and lower_text.find(alias.lower()) >= 0
        ]
        if positions:
            hits.append((min(positions), canonical))

    out: List[str] = []
    for _, brand in sorted(hits, key=lambda item: item[0]):
        if brand not in out:
            out.append(brand)
    return out


def resolve_candidate_brand_name(row: pd.Series) -> str:
    """Prefer brands explicitly mentioned in the comment, then title/content."""
    for field_name in ("review_text", "title", "content"):
        brands = extract_mentioned_brands(row.get(field_name))
        if brands:
            return ",".join(brands)
    return ""


def resolve_event_brand_name(row: pd.Series, event: Dict[str, Any]) -> str:
    """Resolve the brand tied to one extracted symptom event."""
    llm_brand = str(event.get("brand_name", "") or "").strip()
    if llm_brand:
        return llm_brand

    evidence_text = str(event.get("evidence_text", "") or "")
    evidence_brands = extract_mentioned_brands(evidence_text)
    if evidence_brands:
        return ",".join(evidence_brands)

    review_brands = extract_mentioned_brands(row.get("review_text"))
    if len(review_brands) == 1:
        return review_brands[0]

    return ""


# =========================================================
# 9. 日期与哈希工具
# =========================================================

def make_comment_hash(row: pd.Series) -> str:
    raw = (
        f"{row.get('source_id', '')}|"
        f"{row.get('brand_name', '')}|"
        f"{row.get('review_text', '')}|"
        f"{row.get('review_date_raw', '')}"
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def normalize_review_date(value) -> Optional[str]:
    if value is None or pd.isna(value):
        return None

    value = str(value).strip()

    if not value:
        return None

    # 处理类似 5/2/2026、16/2/2026
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
    if m:
        day, month, year = m.groups()
        try:
            dt = datetime(int(year), int(month), int(day))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    # 处理常规日期
    try:
        dt = pd.to_datetime(value, errors="raise")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


# =========================================================
# 10. JSON 解析
# =========================================================

def safe_json_loads(content: str) -> Any:
    if not content:
        return []

    content = content.strip()

    content = re.sub(r"^```json", "", content, flags=re.I).strip()
    content = re.sub(r"^```", "", content).strip()
    content = re.sub(r"```$", "", content).strip()

    try:
        return json.loads(content)
    except Exception:
        pass

    array_match = re.search(r"\[.*\]", content, flags=re.S)
    if array_match:
        return json.loads(array_match.group(0))

    object_match = re.search(r"\{.*\}", content, flags=re.S)
    if object_match:
        return json.loads(object_match.group(0))

    raise ValueError(f"无法解析大模型返回 JSON：{content}")


# =========================================================
# 11. 通义千问客户端
# =========================================================

def _normalize_openai_base_url(url: Optional[str]) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return LLM_BASE_URL
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")]
    return url


def get_qwen_config() -> Dict[str, str]:
    """Resolve Qwen credentials from env first, then legacy project config.yaml."""
    return runtime_config.get_qwen_config()


def get_llm_client() -> OpenAI:
    qwen_config = get_qwen_config()
    if not qwen_config["api_key"]:
        raise RuntimeError(
            "未检测到通义千问 API Key。请设置 DASHSCOPE_API_KEY/QWEN_API_KEY，"
            "或在 csv_mysql_labeling_project/config/config.yaml 的 ocr.qwen_api_key 中配置。"
        )

    return OpenAI(
        api_key=qwen_config["api_key"],
        base_url=qwen_config["base_url"],
        timeout=DEFAULT_LLM_TIMEOUT_SECONDS,
    )


def wait_for_llm_slot() -> None:
    if MAX_LLM_CALLS_PER_MINUTE <= 0:
        return
    min_interval = 60.0 / float(MAX_LLM_CALLS_PER_MINUTE)
    global _last_llm_call_at
    with _llm_rate_lock:
        now = time.monotonic()
        sleep_for = _last_llm_call_at + min_interval - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        _last_llm_call_at = time.monotonic()


# =========================================================
# 12. Prompt 构造
# =========================================================

def build_prompt(
    brand_name: str,
    review_text: str,
    keyword_hits: List[Dict[str, Any]],
    search_keyword: str = "",
    mentioned_brands: Optional[List[str]] = None,
) -> str:
    keyword_hit_text = json.dumps(keyword_hits, ensure_ascii=False)
    mentioned_brand_text = json.dumps(mentioned_brands or [], ensure_ascii=False)

    return f"""
你是一个宠物食品消费者评论结构化标注助手。

你的任务：
从猫粮评论中抽取和猫咪病症、体感变化、健康反馈相关的事件，并输出标准 JSON 数组。

评论中识别到的品牌候选：
{brand_name}

搜索/召回关键词：
{search_keyword}

评论原文：
{review_text}

评论品牌候选列表：
{mentioned_brand_text}

关键词候选命中：
{keyword_hit_text}

你需要判断评论中是否存在和猫粮相关的健康反馈事件。

重要判断规则：

1. 如果评论说“吃了这个品牌/这款粮之后出现问题、变严重、不舒服”，标为“加重”。
2. 如果评论说“吃了这个品牌/这款粮之后好了、改善、没有再出现问题”，标为“改善”。
3. 如果评论表达“不是这个品牌造成的，而是别的粮造成的；换到当前品牌后好了”，当前品牌对应事件标为“改善”。
4. 如果评论只是询问、猜测、担心，或者原因不确定，标为“不确定”。
5. 不要把“没有软便”“没有拉稀”“一点事没有”标成“加重”。
6. 如果评论中同时出现多个症状，需要输出多个事件。
7. 如果评论和病症、健康反馈无关，输出空数组 []。
8. “拉屎臭”“便便臭”不等于软便/拉稀，应单独标为“拉屎臭”。
9. “小疙瘩”“一粒一粒”“不是一长条”更接近“便秘/干硬便”，不要标成“软便/拉稀”。
10. “油尾巴”“种马尾”应归为“皮肤毛发问题-油尾巴”。
11. 如果评论说“吃什么都拉稀/吐，吃当前品牌没事”，应对当前品牌标为“改善”。
12. brand_name 必须是该条症状事件直接相关的品牌，不是搜索关键词。
13. 如果一条评论提到多个品牌，需要判断每个症状事件分别归因到哪个品牌；不要把无关品牌填进去。
14. 如果证据无法判断症状与哪个品牌相关，brand_name 输出空字符串 ""。
15. 如果同一症状分别涉及多个品牌，应输出多条事件，每条事件填写自己的 brand_name。

字段要求：

symptom_category：一级分类，只能从以下选择：
- 消化系统问题
- 皮肤毛发问题
- 眼部问题
- 适口性问题
- 其他健康问题

symptom_name：二级症状，只能从以下选择：
- 软便/拉稀
- 拉屎臭
- 呕吐
- 便秘/干硬便
- 黑下巴
- 油尾巴
- 掉毛
- 瘙痒/过敏
- 泪痕
- 不爱吃
- 其他

effect_direction：影响方向，只能从以下选择：
- 加重
- 改善
- 不确定

其他字段：
- brand_name：与该症状事件直接相关的品牌；只能来自评论原文或品牌候选列表，无法判断则为空字符串
- evidence_text：原文中的证据片段
- confidence：0 到 1 之间的小数

输出要求：
1. 必须输出 JSON 数组。
2. 不要输出任何解释文字。
3. 不要使用 Markdown。
4. 如果无相关事件，输出 []。

输出示例：
[
  {{
    "brand_name": "冠能",
    "symptom_category": "消化系统问题",
    "symptom_name": "软便/拉稀",
    "effect_direction": "改善",
    "evidence_text": "吃别的拉稀软便，自从换了冠能之后再也没软便过",
    "confidence": 0.93
  }}
]
"""


# =========================================================
# 13. 调用通义千问做结构化标注
# =========================================================

def call_llm_for_annotation(
    client: OpenAI,
    brand_name: str,
    review_text: str,
    keyword_hits: List[Dict[str, Any]],
    search_keyword: str = "",
    mentioned_brands: Optional[List[str]] = None,
    max_retries: int = 3
) -> List[Dict[str, Any]]:

    prompt = build_prompt(
        brand_name=brand_name,
        review_text=review_text,
        keyword_hits=keyword_hits,
        search_keyword=search_keyword,
        mentioned_brands=mentioned_brands,
    )
    model_name = get_qwen_config()["model"]

    last_error = None

    for attempt in range(max_retries):
        try:
            wait_for_llm_slot()
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": "你是严谨的中文宠物食品评论结构化标注助手。你只能输出合法 JSON。"
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.1
            )

            content = response.choices[0].message.content
            result = safe_json_loads(content)

            if isinstance(result, dict):
                result = [result]

            if not isinstance(result, list):
                return []

            return clean_llm_events(result)

        except Exception as e:
            last_error = e
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"通义千问标注失败：{last_error}")


# =========================================================
# 14. 清洗大模型返回
# =========================================================

def clean_llm_events(result: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned = []

    for item in result:
        if not isinstance(item, dict):
            continue

        symptom_category = str(item.get("symptom_category", "")).strip()
        symptom_name = str(item.get("symptom_name", "")).strip()
        effect_direction = str(item.get("effect_direction", "")).strip()

        if not symptom_category or not symptom_name or not effect_direction:
            continue

        if symptom_category not in VALID_SYMPTOM_CATEGORIES:
            symptom_category = "其他健康问题"

        if symptom_name not in VALID_SYMPTOM_NAMES:
            symptom_name = "其他"

        if effect_direction not in VALID_EFFECT_DIRECTIONS:
            effect_direction = "不确定"

        try:
            confidence = float(item.get("confidence", 0.0) or 0.0)
        except Exception:
            confidence = 0.0

        confidence = max(0.0, min(1.0, confidence))

        cleaned.append({
            "brand_name": str(item.get("brand_name", "") or "").strip(),
            "symptom_category": symptom_category,
            "symptom_name": symptom_name,
            "effect_direction": effect_direction,
            "evidence_text": str(item.get("evidence_text", "")).strip(),
            "confidence": confidence
        })

    return cleaned


# =========================================================
# 15. 写入结果表
# =========================================================

def insert_events(engine, events: List[Dict[str, Any]]):
    if not events:
        return

    sql = f"""
    INSERT INTO {TARGET_TABLE} (
        source_id,
        comment_hash,
        brand_name,
        review_text,
        symptom_category,
        symptom_name,
        effect_direction,
        review_date_raw,
        review_date,
        processed_at,
        confidence,
        evidence_text,
        hit_keywords,
        model_name,
        prompt_version
    )
    VALUES (
        :source_id,
        :comment_hash,
        :brand_name,
        :review_text,
        :symptom_category,
        :symptom_name,
        :effect_direction,
        :review_date_raw,
        :review_date,
        :processed_at,
        :confidence,
        :evidence_text,
        :hit_keywords,
        :model_name,
        :prompt_version
    )
    ON DUPLICATE KEY UPDATE
        confidence = VALUES(confidence),
        evidence_text = VALUES(evidence_text),
        hit_keywords = VALUES(hit_keywords),
        processed_at = VALUES(processed_at),
        model_name = VALUES(model_name),
        prompt_version = VALUES(prompt_version),
        updated_at = CURRENT_TIMESTAMP;
    """

    with engine.begin() as conn:
        conn.execute(text(sql), events)


def create_disease_review_table(engine, target_table: str = DISEASE_REVIEW_TABLE) -> None:
    target_table = _safe_table(target_table)
    sql = f"""
    CREATE TABLE IF NOT EXISTS `{target_table}` (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,

        source_candidate_id BIGINT NOT NULL,
        comment_hash VARCHAR(64) NOT NULL,
        platform VARCHAR(32),
        external_id VARCHAR(128),
        brand_name VARCHAR(100),
        search_keyword TEXT,
        mentioned_brands TEXT,
        review_text LONGTEXT,

        symptom_category VARCHAR(100),
        symptom_name VARCHAR(100),
        effect_direction VARCHAR(50),

        review_date_raw VARCHAR(100),
        review_date DATE,
        processed_at DATETIME DEFAULT CURRENT_TIMESTAMP,

        confidence DECIMAL(5,4),
        evidence_text TEXT,
        hit_keywords TEXT,

        model_name VARCHAR(100),
        prompt_version VARCHAR(50),

        review_status ENUM('PENDING','APPROVED','REJECTED') NOT NULL DEFAULT 'PENDING',
        reviewer_note TEXT,

        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

        UNIQUE KEY uk_comment_brand_symptom (
            comment_hash,
            brand_name,
            symptom_category,
            symptom_name,
            effect_direction
        ),
        KEY idx_review_status (review_status),
        KEY idx_comment_hash (comment_hash),
        KEY idx_review_date (review_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """
    with engine.begin() as conn:
        conn.execute(text(sql))
        column_rows = conn.execute(
            text(
                """
                SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                """
            ),
            {"table_name": target_table},
        ).mappings().fetchall()
        cols = {row["COLUMN_NAME"] for row in column_rows}
        if "search_keyword" not in cols:
            conn.execute(text(f"ALTER TABLE `{target_table}` ADD COLUMN search_keyword TEXT NULL AFTER brand_name"))
        if "mentioned_brands" not in cols:
            conn.execute(text(f"ALTER TABLE `{target_table}` ADD COLUMN mentioned_brands TEXT NULL AFTER search_keyword"))
        col_meta = {row["COLUMN_NAME"]: row for row in column_rows}
        search_keyword = col_meta.get("search_keyword")
        if search_keyword and (
            str(search_keyword["DATA_TYPE"]).lower() != "text"
            or str(search_keyword["IS_NULLABLE"]).upper() != "YES"
        ):
            conn.execute(text(f"ALTER TABLE `{target_table}` MODIFY COLUMN search_keyword TEXT NULL"))

        brand_name = col_meta.get("brand_name")
        if brand_name and (
            str(brand_name["IS_NULLABLE"]).upper() != "NO"
            or (brand_name["COLUMN_DEFAULT"] not in ("", "''"))
        ):
            conn.execute(
                text(
                    f"""
                    UPDATE `{target_table}`
                    SET brand_name = ''
                    WHERE brand_name IS NULL
                    """
                )
            )
            conn.execute(text(f"ALTER TABLE `{target_table}` MODIFY COLUMN brand_name VARCHAR(100) NOT NULL DEFAULT ''"))

        index_rows = conn.execute(text(f"SHOW INDEX FROM `{target_table}`")).fetchall()
        index_names = {row[2] for row in index_rows}
        if "uk_candidate_symptom" in index_names:
            conn.execute(text(f"ALTER TABLE `{target_table}` DROP INDEX uk_candidate_symptom"))
            index_names.remove("uk_candidate_symptom")
        if "uk_comment_brand_symptom" not in index_names:
            conn.execute(
                text(
                    f"""
                    DELETE t
                    FROM `{target_table}` t
                    JOIN (
                        SELECT
                            comment_hash,
                            brand_name,
                            symptom_category,
                            symptom_name,
                            effect_direction,
                            MIN(id) AS keep_id
                        FROM `{target_table}`
                        GROUP BY
                            comment_hash,
                            brand_name,
                            symptom_category,
                            symptom_name,
                            effect_direction
                        HAVING COUNT(*) > 1
                    ) d
                      ON t.comment_hash = d.comment_hash
                     AND t.brand_name = d.brand_name
                     AND t.symptom_category = d.symptom_category
                     AND t.symptom_name = d.symptom_name
                     AND t.effect_direction = d.effect_direction
                     AND t.id <> d.keep_id
                    """
                )
            )
            conn.execute(
                text(
                    f"""
                    ALTER TABLE `{target_table}`
                    ADD UNIQUE KEY uk_comment_brand_symptom (
                        comment_hash,
                        brand_name,
                        symptom_category,
                        symptom_name,
                        effect_direction
                    )
                    """
                )
            )


def make_candidate_comment_hash(row: pd.Series) -> str:
    raw = f"{row.get('review_text', '')}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_disease_candidates(
    engine,
    *,
    source_table: str = DISEASE_SOURCE_TABLE,
    target_table: str = DISEASE_REVIEW_TABLE,
    limit: Optional[int] = 100,
    min_id: Optional[int] = None,
    max_id: Optional[int] = None,
    skip_existing: bool = True,
    max_comment_chars: int = DEFAULT_MAX_COMMENT_CHARS,
) -> pd.DataFrame:
    source_table = _safe_table(source_table)
    target_table = _safe_table(target_table)

    filters = [
        "c.comment_text IS NOT NULL",
        "TRIM(c.comment_text) <> ''",
        "c.comment_text NOT REGEXP '[\\r\\n]'",
        "CHAR_LENGTH(TRIM(c.comment_text)) <= :max_comment_chars",
    ]
    params: Dict[str, Any] = {"max_comment_chars": int(max_comment_chars)}
    if min_id is not None:
        filters.append("c.id >= :min_id")
        params["min_id"] = int(min_id)
    if max_id is not None:
        filters.append("c.id <= :max_id")
        params["max_id"] = int(max_id)
    if skip_existing:
        filters.append(
            f"""
            NOT EXISTS (
                SELECT 1
                FROM `{target_table}` t
                WHERE t.review_text = TRIM(c.comment_text)
            )
            """
        )

    sql = f"""
    SELECT
        MIN(c.id) AS source_candidate_id,
        GROUP_CONCAT(DISTINCT c.platform ORDER BY c.platform SEPARATOR ',') AS platform,
        MIN(c.external_id) AS external_id,
        GROUP_CONCAT(DISTINCT c.keyword ORDER BY c.keyword SEPARATOR ' | ') AS search_keyword,
        TRIM(c.comment_text) AS review_text,
        MIN(c.event_date) AS review_date,
        MIN(c.event_date) AS review_date_raw,
        MIN(c.title) AS title,
        MIN(c.content) AS content,
        COUNT(*) AS source_candidate_count
    FROM `{source_table}` c
    WHERE {" AND ".join(filters)}
    GROUP BY TRIM(c.comment_text)
    ORDER BY MIN(c.id) ASC
    """
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

    with engine.begin() as conn:
        conn.execute(text("SET SESSION group_concat_max_len = :max_len"), {"max_len": GROUP_CONCAT_MAX_LEN})
        return pd.read_sql(text(sql), conn, params=params)


def insert_disease_review_events(
    engine,
    events: List[Dict[str, Any]],
    *,
    target_table: str = DISEASE_REVIEW_TABLE,
) -> int:
    if not events:
        return 0

    target_table = _safe_table(target_table)
    sql = f"""
    INSERT INTO `{target_table}` (
        source_candidate_id,
        comment_hash,
        platform,
        external_id,
        brand_name,
        search_keyword,
        mentioned_brands,
        review_text,
        symptom_category,
        symptom_name,
        effect_direction,
        review_date_raw,
        review_date,
        processed_at,
        confidence,
        evidence_text,
        hit_keywords,
        model_name,
        prompt_version,
        review_status
    )
    VALUES (
        :source_candidate_id,
        :comment_hash,
        :platform,
        :external_id,
        :brand_name,
        :search_keyword,
        :mentioned_brands,
        :review_text,
        :symptom_category,
        :symptom_name,
        :effect_direction,
        :review_date_raw,
        :review_date,
        :processed_at,
        :confidence,
        :evidence_text,
        :hit_keywords,
        :model_name,
        :prompt_version,
        :review_status
    )
    ON DUPLICATE KEY UPDATE
        confidence = VALUES(confidence),
        evidence_text = VALUES(evidence_text),
        hit_keywords = VALUES(hit_keywords),
        search_keyword = VALUES(search_keyword),
        mentioned_brands = VALUES(mentioned_brands),
        processed_at = VALUES(processed_at),
        model_name = VALUES(model_name),
        prompt_version = VALUES(prompt_version),
        updated_at = CURRENT_TIMESTAMP;
    """
    with engine.begin() as conn:
        result = conn.execute(text(sql), events)
    return int(result.rowcount or 0)


def structure_cat_disease_clues(
    *,
    db: Optional[Dict[str, Any]] = None,
    source_table: str = DISEASE_SOURCE_TABLE,
    target_table: str = DISEASE_REVIEW_TABLE,
    limit: Optional[int] = 100,
    batch_size: int = 20,
    min_id: Optional[int] = None,
    max_id: Optional[int] = None,
    skip_existing: bool = True,
    max_retries: int = 3,
    max_comment_chars: int = DEFAULT_MAX_COMMENT_CHARS,
) -> Dict[str, Any]:
    db_config = _db_config(db)
    source_table = _safe_table(source_table)
    target_table = _safe_table(target_table)

    engine = create_engine(_mysql_url(db_config), pool_pre_ping=True, future=True)
    try:
        create_disease_review_table(engine, target_table=target_table)
        df = load_disease_candidates(
            engine,
            source_table=source_table,
            target_table=target_table,
            limit=limit,
            min_id=min_id,
            max_id=max_id,
            skip_existing=skip_existing,
            max_comment_chars=max_comment_chars,
        )

        if df.empty:
            return {
                "ok": True,
                "source_table": source_table,
                "target_table": target_table,
                "max_comment_chars": int(max_comment_chars),
                "candidate_rows": 0,
                "source_start_id": None,
                "source_end_id": None,
                "processed_rows": 0,
                "event_rows": 0,
                "inserted_or_updated_rows": 0,
                "errors": [],
            }

        df["review_text"] = df["review_text"].fillna("").astype(str)
        source_start_id = int(df["source_candidate_id"].min())
        source_end_id = int(df["source_candidate_id"].max())
        candidate_df = df[df["review_text"].apply(is_candidate_review)].copy()
        if candidate_df.empty:
            return {
                "ok": True,
                "source_table": source_table,
                "target_table": target_table,
                "max_comment_chars": int(max_comment_chars),
                "candidate_rows": int(len(df)),
                "source_start_id": source_start_id,
                "source_end_id": source_end_id,
                "keyword_candidate_rows": 0,
                "processed_rows": 0,
                "event_rows": 0,
                "inserted_or_updated_rows": 0,
                "errors": [],
            }

        client = get_llm_client()
        model_name = get_qwen_config()["model"]

        buffer_events: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        processed_count = 0
        event_count = 0
        written_count = 0

        for _, row in candidate_df.iterrows():
            source_candidate_id = int(row.get("source_candidate_id"))
            review_text = str(row.get("review_text", "") or "")
            search_keyword = str(row.get("search_keyword", "") or "")
            mentioned_brands = extract_mentioned_brands(review_text)
            brand_name = ",".join(mentioned_brands)
            keyword_hits = keyword_recall(review_text)
            hit_keywords = flatten_hit_keywords(keyword_hits)
            comment_hash = make_candidate_comment_hash(row)

            try:
                llm_events = call_llm_for_annotation(
                    client=client,
                    brand_name=brand_name,
                    review_text=review_text,
                    keyword_hits=keyword_hits,
                    search_keyword=search_keyword,
                    mentioned_brands=mentioned_brands,
                    max_retries=max_retries,
                )
            except Exception as exc:
                errors.append(
                    {
                        "source_candidate_id": source_candidate_id,
                        "error": str(exc),
                    }
                )
                continue

            review_date_raw = row.get("review_date_raw")
            review_date = normalize_review_date(row.get("review_date"))
            if not review_date:
                review_date = normalize_review_date(review_date_raw)

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for event in llm_events:
                event_brand_name = resolve_event_brand_name(row, event)
                buffer_events.append(
                    {
                        "source_candidate_id": source_candidate_id,
                        "comment_hash": comment_hash,
                        "platform": row.get("platform"),
                        "external_id": row.get("external_id"),
                        "brand_name": event_brand_name,
                        "search_keyword": search_keyword,
                        "mentioned_brands": ",".join(mentioned_brands),
                        "review_text": review_text,
                        "symptom_category": event["symptom_category"],
                        "symptom_name": event["symptom_name"],
                        "effect_direction": event["effect_direction"],
                        "review_date_raw": str(review_date_raw) if review_date_raw is not None else None,
                        "review_date": review_date,
                        "processed_at": now,
                        "confidence": event.get("confidence", 0.0),
                        "evidence_text": event.get("evidence_text", ""),
                        "hit_keywords": hit_keywords,
                        "model_name": model_name,
                        "prompt_version": PROMPT_VERSION,
                        "review_status": "PENDING",
                    }
                )

            processed_count += 1
            event_count += len(llm_events)

            if len(buffer_events) >= batch_size:
                written_count += insert_disease_review_events(
                    engine,
                    buffer_events,
                    target_table=target_table,
                )
                buffer_events = []

        written_count += insert_disease_review_events(
            engine,
            buffer_events,
            target_table=target_table,
        )

        return {
            "ok": len(errors) == 0,
            "source_table": source_table,
            "target_table": target_table,
            "max_comment_chars": int(max_comment_chars),
            "candidate_rows": int(len(df)),
            "source_start_id": source_start_id,
            "source_end_id": source_end_id,
            "keyword_candidate_rows": int(len(candidate_df)),
            "processed_rows": processed_count,
            "event_rows": event_count,
            "inserted_or_updated_rows": written_count,
            "skipped_existing": bool(skip_existing),
            "errors": errors[:50],
            "error_count": len(errors),
        }
    finally:
        engine.dispose()


# =========================================================
# 16. 可选：导出结果到 CSV
# =========================================================

def export_result_to_csv(engine, output_path: str = "review_symptom_event_result.csv"):
    sql = f"""
    SELECT
        id,
        comment_hash,
        brand_name,
        review_text,
        symptom_category,
        symptom_name,
        effect_direction,
        review_date_raw,
        review_date,
        processed_at
    FROM {TARGET_TABLE}
    ORDER BY id ASC
    """

    df = pd.read_sql(sql, engine)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")

    print(f"结果已导出：{output_path}")


# =========================================================
# 17. 主流程
# =========================================================

def run_pipeline(
    limit: Optional[int] = 100,
    batch_size: int = 20,
    export_csv: bool = True
):
    engine = get_mysql_engine()
    create_target_table(engine)

    client = get_llm_client()
    model_name = get_qwen_config()["model"]

    df = load_reviews(engine, limit=limit)

    print(f"读取原始评论数：{len(df)}")

    if df.empty:
        print("没有读取到评论，流程结束。")
        return

    df["review_text"] = df["review_text"].fillna("").astype(str)

    candidate_df = df[df["review_text"].apply(is_candidate_review)].copy()

    print(f"关键词召回候选评论数：{len(candidate_df)}")

    if candidate_df.empty:
        print("没有召回到候选病症评论，流程结束。")
        return

    buffer_events = []
    processed_count = 0
    total_event_count = 0

    for _, row in candidate_df.iterrows():
        source_id = str(row.get("source_id", ""))
        brand_name = str(row.get("brand_name", "") or "")
        review_text = str(row.get("review_text", "") or "")
        review_date_raw = row.get("review_date_raw", None)

        comment_hash = make_comment_hash(row)
        keyword_hits = keyword_recall(review_text)
        hit_keywords = flatten_hit_keywords(keyword_hits)

        try:
            llm_events = call_llm_for_annotation(
                client=client,
                brand_name=brand_name,
                review_text=review_text,
                keyword_hits=keyword_hits
            )
        except Exception as e:
            print(f"[失败] source_id={source_id}, comment_hash={comment_hash}, error={e}")
            continue

        review_date = normalize_review_date(row.get("review_date", None))

        if not review_date:
            review_date = normalize_review_date(review_date_raw)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for event in llm_events:
            buffer_events.append({
                "source_id": source_id,
                "comment_hash": comment_hash,
                "brand_name": brand_name,
                "review_text": review_text,
                "symptom_category": event["symptom_category"],
                "symptom_name": event["symptom_name"],
                "effect_direction": event["effect_direction"],
                "review_date_raw": str(review_date_raw) if review_date_raw is not None else None,
                "review_date": review_date,
                "processed_at": now,
                "confidence": event.get("confidence", 0.0),
                "evidence_text": event.get("evidence_text", ""),
                "hit_keywords": hit_keywords,
                "model_name": model_name,
                "prompt_version": PROMPT_VERSION
            })

        processed_count += 1
        total_event_count += len(llm_events)

        if processed_count % batch_size == 0:
            insert_events(engine, buffer_events)
            print(
                f"已处理候选评论：{processed_count} / {len(candidate_df)}，"
                f"累计抽取事件数：{total_event_count}"
            )
            buffer_events = []

    insert_events(engine, buffer_events)

    print(
        f"处理完成。候选评论数：{len(candidate_df)}，"
        f"成功处理：{processed_count}，"
        f"抽取事件数：{total_event_count}"
    )

    if export_csv:
        export_result_to_csv(engine)


# =========================================================
# 18. 入口
# =========================================================

if __name__ == "__main__":
    """
    第一次建议先跑 100 条：
    run_pipeline(limit=100)

    确认结果没问题后，再跑全量：
    run_pipeline(limit=None)
    """

    run_pipeline(
        limit=100,
        batch_size=20,
        export_csv=True
    )
