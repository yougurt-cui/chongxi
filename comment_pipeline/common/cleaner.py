"""Business-neutral normalization and basic product recognition."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
HASHTAG_RE = re.compile(r"#[^#\n]{1,40}#")
EMOTICON_RE = re.compile(r"\[[^\[\]\n]{1,16}\]")
MENTION_RE = re.compile(r"@[A-Za-z0-9_\u4e00-\u9fff.·\-]{0,30}")
EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # 区域指示符（国旗）
    "\U0001F300-\U0001F5FF"  # 杂项符号与象形图
    "\U0001F600-\U0001F64F"  # 表情
    "\U0001F680-\U0001F6FF"  # 交通与地图符号
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2190-\u21FF"          # 箭头
    "\u2600-\u27BF"          # 杂项符号与装饰符号
    "\u2B00-\u2BFF"
    "\uFE0F\u20E3"           # 变体选择符与组合键帽
    "]"
)
MEANINGFUL_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
LOW_VALUE_RE = re.compile(
    r"^(?:哈+|呵+|嘿+|嗯+|哦+|啊+|666+|赞+|顶+|路过|沙发|来了|收到|谢谢|感谢|好滴|好的|ok|nice)[~～!！?？。,.，\s]*$",
    re.IGNORECASE,
)
INTERJECTION_ONLY_RE = re.compile(r"^[\W_]*[呃哦嗯啊诶唉呀哟哈嘿唉][\W_]*$")
# 实义字符数低于该值视为低信息量，不进入偏好抽取（可按需调整）。
LOW_INFO_MIN_CHARS = 6

PRODUCT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"自动(?:投食器|喂食器|零食发射器)", "宠物喂食器"),
    (r"(?:猫粮|主粮|干粮|湿粮|冻干粮|风干粮|烘焙粮|处方粮|幼猫粮|成猫粮|泌尿粮|肠胃粮)", "猫粮"),
    (r"(?:踢踢鱼|猫薄荷(?:毛绒)?玩具|毛绒小鱼)", "猫玩具"),
    (r"(?:逗猫棒|轨道球|磨牙玩具|漏食玩具|激光玩具|自动互动玩具)", "猫玩具"),
    (r"(?:猫抓板|猫抓柱|猫爬架)", "猫抓用品"),
    (r"(?:猫隧道|宠物隧道)", "猫玩具"),
    (r"(?:猫砂|猫砂盆|猫砂铲)", "猫砂用品"),
    (r"(?:宠物服|猫衣服|狗衣服|宠物衣服)", "宠物服饰"),
    (r"(?:饮水机|喂食器|食盆|猫碗|宠物碗)", "宠物喂养用品"),
)


@dataclass(frozen=True)
class CleanedComment:
    platform: str
    source_table: str
    source_id: str
    source_row_id: int | None
    raw_text: str
    clean_text: str
    text_hash: str
    brand: str
    product_name: str
    product_category: str
    source_title: str
    source_content: str
    source_keyword: str
    source_created_at: str
    source_like_count: int | None


def strip_platform_noise(text: str) -> str:
    """剥离平台噪音：话题标签、表情占位符、emoji 字符、@提及。

    顺序有意为之：先删表情字符，再删 @提及，这样"@EMILY🌟"这类
    昵称里夹 emoji 的提及才能被整段清掉。
    """
    text = HASHTAG_RE.sub(" ", text)
    text = EMOTICON_RE.sub(" ", text)
    text = EMOJI_RE.sub(" ", text)
    text = MENTION_RE.sub(" ", text)
    return text


def normalize_text(value: Any, strip_noise: bool = True) -> str:
    text = ZERO_WIDTH_RE.sub("", str(value or ""))
    text = URL_RE.sub(" ", text)
    if strip_noise:
        text = strip_platform_noise(text)
    return re.sub(r"\s+", " ", text).strip()


def substantive_length(text: str) -> int:
    """去掉标点、空白和符号后剩余的实义字符数。"""
    return len(re.sub(r"[\W_]+", "", text or ""))


def is_low_information(text: str, min_chars: int = LOW_INFO_MIN_CHARS) -> bool:
    """判断文本是否低信息量：过于短小，或只剩语气词。"""
    if substantive_length(text) < min_chars:
        return True
    return bool(INTERJECTION_ONLY_RE.match((text or "").strip()))


def is_meaningful(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(len(compact) >= 2 and MEANINGFUL_RE.search(compact) and not LOW_VALUE_RE.fullmatch(compact))


def recognize_brand(text: str, brand_aliases: dict[str, str]) -> str:
    matches = [
        (alias, standard)
        for alias, standard in brand_aliases.items()
        if alias and re.search(re.escape(alias), text, flags=re.IGNORECASE)
    ]
    return max(matches, key=lambda pair: len(pair[0]))[1] if matches else ""


def recognize_product(
    text: str, product_aliases: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, str]:
    alias_matches = [
        (alias, value)
        for alias, value in (product_aliases or {}).items()
        if alias and re.search(re.escape(alias), text, flags=re.IGNORECASE)
    ]
    if alias_matches:
        _alias, (standard_name, category) = max(alias_matches, key=lambda pair: len(pair[0]))
        return standard_name, category
    for pattern, category in PRODUCT_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(0), category
    return "", ""


def clean_source_row(
    spec: Any, row: dict[str, Any], brand_aliases: dict[str, str],
    product_aliases: dict[str, tuple[str, str]] | None = None,
) -> CleanedComment | None:
    raw_text = str(row.get("comment_text") or "")
    clean_text = normalize_text(raw_text)
    if not is_meaningful(clean_text):
        return None
    external_id = normalize_text(row.get(spec.external_id_col), strip_noise=False)
    row_id = row.get(spec.id_col)
    source_id = external_id or f"row:{row_id}"
    brand = recognize_brand(clean_text, brand_aliases)
    product_name, product_category = recognize_product(clean_text, product_aliases)
    return CleanedComment(
        platform=spec.platform,
        source_table=spec.table,
        source_id=source_id,
        source_row_id=int(row_id) if row_id is not None else None,
        raw_text=raw_text,
        clean_text=clean_text,
        text_hash=hashlib.sha256(clean_text.casefold().encode("utf-8")).hexdigest(),
        brand=brand,
        product_name=product_name,
        product_category=product_category,
        source_title=normalize_text(row.get(spec.title_col)),
        source_content=normalize_text(row.get(spec.content_col)),
        source_keyword=normalize_text(row.get(spec.keyword_col)),
        source_created_at=normalize_text(row.get(spec.time_col)),
        source_like_count=int(row.get(spec.like_col)) if row.get(spec.like_col) not in (None, "") else None,
    )
