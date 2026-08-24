#!/usr/bin/env python
"""Incrementally filter raw comments that contain cat-food choice intent."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


SOURCE_TABLES = ("xiaohongshu_raw_comments", "douyin_raw_comments")
OUTPUT_TABLE = "catfood_choice_comments_filtered"
ARTIFACT_ROOT = BASE_DIR / "var" / "catfood_choice_comment_artifacts"
BATCH_SIZE = 1000

CATFOOD_TERMS = (
    "猫粮|主粮|干粮|湿粮|冻干粮|风干粮|烘焙粮|处方粮|幼猫粮|成猫粮|减肥粮|泌尿粮|肠胃粮|粮"
)
CAT_TERMS = (
    "猫|咪|喵|崽|主子|毛孩子|布偶|德文|英短|美短|缅因|幼猫|成猫|绝育|玻璃胃|软便|拉稀|便秘|泌尿|结石|黑下巴"
)
BRAND_TERMS = (
    "皇家|冠能|领先|渴望|爱肯拿|百利|鲜朗|素力高|金素|发米娜|弗列加特|费列加特|伯纳天纯|网易严选|"
    "江小傲|阿飞和巴弟|帕特|巅峰|滋益巅峰|ZIWI|K9|麦富迪|顽皮|乖宝|海洋之星|比瑞吉|耐威克|"
    "高爷家|坦克小希|诚实一口|鲜肉主义|蓝馔|纽顿|GO|NOW|纽翠斯|自然光|天衡宝|希尔斯|普瑞纳|醇粹|虎太郎"
)
# 运行时由 load_brand_terms() 覆盖：从 catfood_standard_brand + _alias 表加载，含别名
_BRAND_PATTERN_CACHE: str | None = None


def load_brand_terms() -> str:
    """从 catfood_standard_brand 和 catfood_standard_brand_alias 表加载所有品牌名和别名，构建正则。

    Returns:
        形如 "皇家|冠能|..." 的正则字符串；DB 不可用时回退到内置 BRAND_TERMS。
    """
    global _BRAND_PATTERN_CACHE
    if _BRAND_PATTERN_CACHE is not None:
        return _BRAND_PATTERN_CACHE
    names: set[str] = set()
    try:
        conn = connect_mysql()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT standard_brand_name FROM catfood_standard_brand WHERE active=1")
                for r in cur.fetchall():
                    v = (r.get("standard_brand_name") or "").strip()
                    if v:
                        names.add(v)
                cur.execute("SELECT alias_name FROM catfood_standard_brand_alias WHERE active=1")
                for r in cur.fetchall():
                    v = (r.get("alias_name") or "").strip()
                    if v:
                        names.add(v)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[load_brand_terms] fallback to built-in BRAND_TERMS: {exc}", file=sys.stderr)
        _BRAND_PATTERN_CACHE = BRAND_TERMS
        return _BRAND_PATTERN_CACHE
    # 长度降序拼接，避免短词先匹配掉长词；转义避免正则特殊字符
    escaped_sorted = sorted((re.escape(n) for n in names), key=len, reverse=True)
    _BRAND_PATTERN_CACHE = "|".join(escaped_sorted) if escaped_sorted else BRAND_TERMS
    return _BRAND_PATTERN_CACHE


# Condition 词表：从 adapters/cat_disease.py 的 SYMPTOM_KEYWORDS 内联（避免 sqlalchemy 依赖）
# 覆盖：消化系统/皮肤毛发/眼部/适口性 等所有二级症状关键词
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
            "泪痕", "眼屎", "眼睛分泌物", "眼周发红"
        ]
    },
    "适口性问题": {
        "不爱吃": [
            "不爱吃", "不吃", "挑食", "闻了就走", "适口性差",
            "不肯吃", "吃得少"
        ]
    },
    "泌尿系统问题": {
        "尿结晶/尿路问题": [
            "泌尿", "尿闭", "尿血", "血尿", "尿结晶", "结晶尿", "尿路结晶",
            "尿结石", "膀胱结石", "肾结石", "膀胱炎", "尿路感染", "尿频",
            "尿不出", "尿不出来", "排尿困难", "尿少", "乱尿", "蹲猫砂盆"
        ]
    },
    "体重与代谢问题": {
        "肥胖/体重管理": [
            "肥胖", "超重", "体重超标", "绝育后发胖", "绝育后长胖", "减肥",
            "控制体重", "体重管理", "易胖", "太胖", "虚胖"
        ],
        "增重/长肉": [
            "长肉", "长胖", "增重", "增肥", "养胖", "喂胖", "吃胖", "变胖",
            "长体重", "体重增长", "体重增加", "涨体重", "发腮", "爆腮", "长腮",
            "怎么长肉", "如何长肉", "吃什么长肉", "吃什么长胖"
        ],
        "消瘦/不长肉": [
            "不长肉", "长不胖", "太瘦", "偏瘦", "消瘦", "体重下降", "掉秤"
        ]
    },
    "口腔问题": {
        "口臭/口腔问题": [
            "口臭", "嘴臭", "牙龈红肿", "牙龈炎", "口炎", "牙结石", "流口水"
        ]
    }
}

# 单独命中时容易落入人类、狗或一般生活语境的宽泛词。它们仍参与高召回，
# 但没有猫/猫粮上下文时只标为 low confidence。
AMBIGUOUS_CONDITION_TERMS = {
    "吐", "吐了", "反胃", "痒", "挠", "不吃", "不爱吃", "吃得少",
    "秃", "掉毛", "脱毛", "过敏", "拉不出来", "特别臭", "臭臭", "粉刺", "红疹",
    "尿少", "乱尿", "太胖", "减肥", "太瘦", "偏瘦", "掉秤", "流口水",
    "长肉", "长胖", "增重", "增肥", "养胖", "喂胖", "吃胖", "变胖", "发腮", "爆腮", "长腮",
}

CAT_ENTITY_TERMS = (
    r"猫|猫咪|猫猫|小猫|幼猫|成猫|老年猫|喵|主子|猫崽|咪子|"
    r"布偶|德文|英短|美短|缅因|拿破仑|加菲"
)


def _flatten_symptom_keywords() -> str:
    flat: set[str] = set()
    for sub in SYMPTOM_KEYWORDS.values():
        for keywords in sub.values():
            flat.update(keywords)
    # 长度降序拼接，避免短词先匹配
    escaped_sorted = sorted((re.escape(k) for k in flat), key=len, reverse=True)
    return "|".join(escaped_sorted)


NEED_SYMPTOM_TERMS = _flatten_symptom_keywords()
CHOICE_TERMS = (
    "推荐|求推|求推荐|求安利|求链接|安利|有没有|有无|哪款|哪个|哪种|哪家|什么牌子|吃什么|买什么|"
    "什么粮|换什么|怎么选|如何选|怎么挑|如何挑|选什么|选哪个|选哪款|哪个好|哪个牌子|适合吗|"
    "可以吗|能吃吗|能喂吗|靠谱不|不翻车|避雷|踩雷|纠结|二选一|对比|vs|VS|平替|"
    "性价比|预算|试吃装|蹲一个|蹲|求助|请问|想问|问问"
)
SPECIAL_NEED_TERMS = (
    "软便|拉稀|便秘|呕吐|肠胃|玻璃胃|黑下巴|过敏|泌尿|结石|肾|尿闭|减肥|长肉|美毛|绝育|"
    "幼猫|成猫|老年|布偶|德文|英短|美短|缅因"
)
ADVICE_TERMS = (
    "选.*粮|粮.*选|不要.*粮|少吃.*粮|适合.*(猫|咪|幼猫|成猫|布偶|德文)|推荐.*粮|粮.*推荐"
)
NOISE_TERMS = (
    "托盘|碗架|饭碗|水碗|猫砂盆|猫砂铲|猫窝|猫抓板|航空箱|背包|玩具|梳子|剪指甲|饮水机|过滤芯"
)


# === 4-intent classification patterns ===
# Need: 用户表达症状/功能需求/喂养困扰，且在寻求解决方案（或未发生实际使用）
# 注意：体质词（幼猫/成猫/布偶等）不独立触发 Need，避免"幼猫选哪个"被误判
# NEED_SYMPTOM_TERMS 已由上方 _flatten_symptom_keywords() 从 SYMPTOM_KEYWORDS 派生
NEED_CONSTITUTION_TERMS = (
    r"幼猫|成猫|老年猫|布偶|德文|英短|美短|缅因|拿破仑|加菲|"
    r"绝育|未绝育|肥胖|偏瘦|长肉|减肥|发腮|美毛|长胖|不长肉"
)
NEED_FUNCTION_TERMS = (
    r"低敏|单一肉源|处方粮|泌尿粮|肠胃粮|减肥粮|"
    r"敏感体质|过敏体质"
)
# 显式需求表达词：用于区分"正在求解决方案" vs "已发生使用后的效果描述"
NEED_REQUEST_TERMS = (
    r"求|想找|需要|有没有适合|有没有推荐|有没有什么|怎么办|求助|"
    r"有什么.{0,6}粮|有没有.{0,6}粮|适合.{0,10}的粮|"
    r"想换.{0,10}粮|想买.{0,10}粮|想找.{0,10}粮"
)

# Decision: 求推荐/产品比较/价格/购买理由/选粮建议/配方选择
DECISION_TERMS = (
    r"推荐|求推|求推荐|求安利|安利|有没有|有无|"
    r"哪款|哪个|哪种|哪家|什么牌子|买什么|吃什么粮|换什么|"
    r"怎么选|如何选|怎么挑|如何挑|选什么|选哪个|选哪款|哪个好|哪个牌子|"
    r"适合吗|可以吗|能吃吗|能喂吗|靠谱不|不翻车|避雷|踩雷|纠结|"
    r"二选一|对比|vs|VS|性价比|预算|试吃|蹲一个|蹲|求助|请问|想问|问问|"
    r"无谷|高蛋白|低碳水|配料|成分|配方|"
    r"好嘛|好吗|好不好|怎么样|咋样|咋样啊"
)
DECISION_PURCHASE_TERMS = (
    r"求链接|链接|官旗|旗舰|真假|正品|哪里买|哪买|旗舰店|活动|打折|便宜|贵"
)
DECISION_ADVICE_TERMS = (
    r"选[^，。！？\s]{0,8}粮|粮[^，。！？\s]{0,8}选|不要[^，。！？\s]{0,8}粮|"
    r"少吃[^，。！？\s]{0,8}粮|推荐[^，。！？\s]{0,8}粮|粮[^，。！？\s]{0,8}推荐"
)

# Experience: 已发生实际使用，描述产品效果/反应
# 要求同时命中「使用动作」和「效果反馈」才算 Experience，避免"一换粮就软便"这类纯症状描述被误判
EXPERIENCE_USE_TERMS = (
    r"吃了|喂了|在吃|我家吃|一直吃|试了|试吃过|试过|吃过|买过|入了|入手|吃完|"
    r"喂着|吃着|我家喂|从小喂|喂到大|从小吃|一直喂|一直在喂|一直在吃|"
    r"在喂|买的是|买的|我家是吃|我家在喂|"
    r"(?:吃|喂|试).{0,15}(?:周|月|天|星期|年|一阵|一段)"
)
EXPERIENCE_OUTCOME_TERMS = (
    r"适口|爱吃|不吃|不爱吃|挑食|油腻|太油|不油|很油|油大|拉肚子|拉稀|软便|稀便|便秘|"
    r"呕吐|吐|黑下巴|长胖|长肉|瘦了|毛发变|便便|大便|便臭|口臭|"
    r"改善|缓解|加重|稳定|适口性好|适口性差|效果好|效果差|"
    r"放心|靠谱|不错|挺好|可以|爱吃|圆润|胖|圆润饱满|吃.好|吃.{0,3}香|吃.{0,3}很好"
)

# Switch: A→B 产品迁移（明确换粮动作或意图，排除"换着吃"交替、纯因果描述）
SWITCH_TERMS = (
    r"换成|换到|改喂|"
    r"想换(?!着)|想换粮|打算换|准备换|换什么粮|换什么猫粮|"
    r"之前.{0,30}换|以前.{0,30}换|原先.{0,30}换|"
    r"从.{0,15}换|把.{0,15}换成|"
    r"不吃了|不喂了|放弃.{0,10}粮|"
    r"平替|替代|"
    r"换.{0,10}回购|回购.{0,10}换|"
    r"换粮.{0,30}(?:推荐|哪|什么|安利)"
)


@dataclass(frozen=True)
class SourceSpec:
    table: str
    platform: str
    id_col: str
    external_id_col: str
    title_col: str
    content_col: str
    like_col: str
    time_col: str
    keyword_col: str


SOURCE_SPECS = {
    "xiaohongshu_raw_comments": SourceSpec(
        table="xiaohongshu_raw_comments",
        platform="xiaohongshu",
        id_col="id",
        external_id_col="external_id",
        title_col="title",
        content_col="content",
        like_col="like_count",
        time_col="comment_time",
        keyword_col="query_keyword",
    ),
    "douyin_raw_comments": SourceSpec(
        table="douyin_raw_comments",
        platform="douyin",
        id_col="id",
        external_id_col="external_id",
        title_col="post_title",
        content_col="post_content",
        like_col="post_like_count",
        time_col="comment_date",
        keyword_col="search_keyword",
    ),
}


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"Unsafe identifier: {name}")
    return f"`{name}`"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def re_search(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def find_condition_matches(text: Any) -> list[tuple[str, str, str]]:
    """Return unique (category, symptom, keyword) condition matches."""
    value = normalize_text(text)
    matches: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    has_failure_to_gain = re_search(r"不长肉|长不胖|怎么都不胖|一直不胖", value)
    has_excess_weight = re_search(
        r"肥胖|超重|体重超标|绝育后发胖|绝育后长胖|减肥|控制体重|体重管理|易胖|太胖|虚胖",
        value,
    )
    for category, symptom_map in SYMPTOM_KEYWORDS.items():
        for symptom, keywords in symptom_map.items():
            if symptom == "增重/长肉" and (has_failure_to_gain or has_excess_weight):
                continue
            hit = next(
                (keyword for keyword in sorted(keywords, key=len, reverse=True) if re_search(re.escape(keyword), value)),
                None,
            )
            if hit and (category, symptom) not in seen:
                seen.add((category, symptom))
                matches.append((category, symptom, hit))
    return matches


def condition_metadata(comment_text: Any, source_context: Any = "") -> dict[str, Any]:
    """Classify condition recall confidence using comment and parent-post context."""
    text = normalize_text(comment_text)
    context = normalize_text(source_context)
    matches = find_condition_matches(text)
    if not matches:
        return {"mentions_condition": False, "confidence": "", "categories": "", "symptoms": "", "keywords": ""}

    comment_has_cat = re_search(CAT_ENTITY_TERMS, text)
    comment_has_food = re_search(CATFOOD_TERMS, text) or re_search(load_brand_terms(), text)
    context_has_cat = re_search(CAT_ENTITY_TERMS, context)
    context_has_food = re_search(CATFOOD_TERMS, context) or re_search(load_brand_terms(), context)
    all_ambiguous = all(keyword in AMBIGUOUS_CONDITION_TERMS for _, _, keyword in matches)
    if comment_has_cat or comment_has_food or context_has_cat or context_has_food:
        confidence = "high"
    elif all_ambiguous:
        confidence = "low"
    else:
        confidence = "medium"
    return {
        "mentions_condition": True,
        "confidence": confidence,
        "categories": "、".join(dict.fromkeys(category for category, _, _ in matches)),
        "symptoms": "、".join(dict.fromkeys(symptom for _, symptom, _ in matches)),
        "keywords": "、".join(dict.fromkeys(keyword for _, _, keyword in matches)),
    }


def find_named_signals(text: str) -> tuple[list[str], list[str], int]:
    signals: list[str] = []
    intents: list[str] = []
    score = 0
    has_catfood_term = re_search(CATFOOD_TERMS, text)
    has_cat_term = re_search(CAT_TERMS, text)
    has_brand = re_search(load_brand_terms(), text)
    has_choice = re_search(CHOICE_TERMS, text)
    has_special_need = re_search(SPECIAL_NEED_TERMS, text)
    has_advice = re_search(ADVICE_TERMS, text)
    has_noise = re_search(NOISE_TERMS, text)

    if has_catfood_term:
        signals.append("猫粮/粮食语境")
        score += 2
    if has_cat_term:
        signals.append("猫/症状语境")
        score += 1
    if has_brand:
        signals.append("猫粮品牌")
        score += 2
    if has_choice:
        signals.append("选择/推荐表达")
        score += 3
    if has_special_need:
        signals.append("特殊需求/症状")
        score += 1
    if has_advice:
        signals.append("选粮建议表达")
        score += 2
    if has_noise:
        signals.append("疑似非猫粮物品")
        score -= 2

    # === 4-intent classification: Need / Decision / Experience / Switch ===
    has_need_symptom = re_search(NEED_SYMPTOM_TERMS, text)
    has_need_function = re_search(NEED_FUNCTION_TERMS, text)
    has_need_request = re_search(NEED_REQUEST_TERMS, text)
    has_experience_use = re_search(EXPERIENCE_USE_TERMS, text)
    has_experience_outcome = re_search(EXPERIENCE_OUTCOME_TERMS, text)

    # Need: 症状/功能词 + (需求词 OR 没有使用动作)
    # 体质词不独立触发 Need（避免"幼猫选哪个"被误判）
    # 如果已在讲使用体验（吃了A软便），没有显式需求词则不算 Need，归 Experience
    has_need_context = has_need_symptom or has_need_function
    if has_need_context and (has_need_request or not has_experience_use):
        intents.append("Need")
    # Decision: 求推荐/产品比较/价格/购买理由/选粮建议
    if (
        re_search(DECISION_TERMS, text)
        or re_search(DECISION_PURCHASE_TERMS, text)
        or re_search(DECISION_ADVICE_TERMS, text)
    ):
        intents.append("Decision")
    # Experience: 已发生实际使用（需同时有使用动作 + 效果反馈）
    if has_experience_use and has_experience_outcome:
        intents.append("Experience")
    # Switch: A→B 产品迁移
    if re_search(SWITCH_TERMS, text):
        intents.append("Switch")

    # Experience/Switch 也算强选择信号，对应加分（让分享使用结果/换粮过程类评论也能入库）
    if "Experience" in intents:
        signals.append("使用体验反馈")
        score += 3
    if "Switch" in intents:
        signals.append("换粮迁移")
        score += 2
    return signals, intents, score


def is_choice_comment(comment_text: Any, source_context: Any = "") -> tuple[bool, list[str], list[str], int, bool, bool]:
    text = normalize_text(comment_text)
    if not text:
        return False, [], [], 0, False, False
    signals, intents, score = find_named_signals(text)
    # 新维度：是否提到品牌 / 是否提到病症
    mentions_brand = "猫粮品牌" in signals
    condition = condition_metadata(text, source_context)
    mentions_condition = bool(condition["mentions_condition"])
    # 病症评论本身就是用户需求信号。它不应因为没有同时提到猫粮、品牌或
    # “怎么选”而被丢弃，否则会系统性低估软便、呕吐、黑下巴等需求规模。
    if mentions_condition:
        if not intents:
            intents.append("Need")
        return True, signals, intents, score, mentions_brand, mentions_condition
    has_catfood_context = (
        "猫粮/粮食语境" in signals
        or "猫粮品牌" in signals
        or (
            "猫/症状语境" in signals
            and re_search("什么牌子|买什么|换什么|哪款|哪个好|哪个牌子|怎么选|如何选|选.*粮|粮.*选", text)
        )
    )
    has_choice_signal = (
        "选择/推荐表达" in signals
        or "选粮建议表达" in signals
        or (re_search("适合|可以|能吃|能喂", text) and ("特殊需求/症状" in signals or "猫粮品牌" in signals))
        # Experience/Switch 也是有效的选择信号：用户在分享使用结果或迁移过程
        or "Experience" in intents
        or "Switch" in intents
    )
    if not has_catfood_context or not has_choice_signal:
        return False, signals, intents, score, mentions_brand, mentions_condition
    weak_only = re.fullmatch(r"(求链接|蹲一个|蹲|链接|哪买|哪里买)[。！？!?~～\s]*", text)
    if weak_only and not ("猫粮/粮食语境" in signals or "猫粮品牌" in signals):
        return False, signals, intents, score, mentions_brand, mentions_condition
    return score >= 5, signals, intents or ["Decision"], score, mentions_brand, mentions_condition


def connect_mysql(cursorclass=pymysql.cursors.DictCursor):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, autocommit=False, cursorclass=cursorclass)


def selected_columns(spec: SourceSpec) -> str:
    cols = [
        spec.id_col,
        spec.external_id_col,
        spec.title_col,
        spec.content_col,
        spec.like_col,
        spec.time_col,
        spec.keyword_col,
        "comment_text",
    ]
    return ", ".join(quote_ident(col) for col in cols)


def ensure_output_table(conn, output_table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(output_table)} (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              run_id VARCHAR(64) NOT NULL,
              source_platform VARCHAR(32) NOT NULL,
              source_schema VARCHAR(64) NOT NULL,
              source_table VARCHAR(64) NOT NULL,
              source_row_id BIGINT NULL,
              external_id VARCHAR(255) NULL,
              source_record_key VARCHAR(255) NOT NULL,
              comment_text LONGTEXT NOT NULL,
              normalized_text LONGTEXT NOT NULL,
              intent_labels VARCHAR(255) NOT NULL,
              matched_signals VARCHAR(500) NOT NULL,
              choice_score INT NOT NULL,
              mentions_brand TINYINT(1) NOT NULL DEFAULT 0,
              mentions_condition TINYINT(1) NOT NULL DEFAULT 0,
              condition_confidence VARCHAR(16) NOT NULL DEFAULT '',
              condition_categories VARCHAR(255) NOT NULL DEFAULT '',
              condition_symptoms VARCHAR(500) NOT NULL DEFAULT '',
              condition_keywords VARCHAR(500) NOT NULL DEFAULT '',
              source_title TEXT NULL,
              source_content MEDIUMTEXT NULL,
              source_like_count INT NULL,
              source_comment_time VARCHAR(64) NULL,
              source_keyword VARCHAR(255) NULL,
              inserted_at DATETIME NOT NULL,
              KEY idx_source (source_platform, source_row_id),
              KEY idx_score (choice_score),
              KEY idx_intent (intent_labels),
              KEY idx_mentions_brand (mentions_brand),
              KEY idx_mentions_condition (mentions_condition)
            ) DEFAULT CHARSET=utf8mb4
            """
        )
        # 兼容已存在的旧表：补字段（忽略已存在时报错）
        for col_def in (
            "ADD COLUMN mentions_brand TINYINT(1) NOT NULL DEFAULT 0",
            "ADD COLUMN mentions_condition TINYINT(1) NOT NULL DEFAULT 0",
            "ADD COLUMN source_record_key VARCHAR(255) NULL AFTER external_id",
            "ADD COLUMN condition_confidence VARCHAR(16) NOT NULL DEFAULT '' AFTER mentions_condition",
            "ADD COLUMN condition_categories VARCHAR(255) NOT NULL DEFAULT '' AFTER condition_confidence",
            "ADD COLUMN condition_symptoms VARCHAR(500) NOT NULL DEFAULT '' AFTER condition_categories",
            "ADD COLUMN condition_keywords VARCHAR(500) NOT NULL DEFAULT '' AFTER condition_symptoms",
        ):
            try:
                cur.execute(f"ALTER TABLE {quote_ident(output_table)} {col_def}")
            except pymysql.err.OperationalError as exc:
                # 1060: Duplicate column name — 字段已存在，忽略
                if exc.args and exc.args[0] != 1060:
                    raise
        # 为旧表回填稳定源记录键。优先使用各平台唯一的 external_id；仅在
        # external_id 缺失时退回 source_row_id。这样 id=0 的小红书记录也可区分。
        cur.execute(
            f"""
            UPDATE {quote_ident(output_table)}
            SET source_record_key = COALESCE(NULLIF(external_id, ''), CONCAT('row:', source_row_id))
            WHERE source_record_key IS NULL OR source_record_key = ''
            """
        )
        # 补索引（忽略已存在时报错）
        for idx_def in (
            "ADD KEY idx_mentions_brand (mentions_brand)",
            "ADD KEY idx_mentions_condition (mentions_condition)",
            "ADD UNIQUE KEY uq_source_record (source_platform, source_table, source_record_key)",
        ):
            try:
                cur.execute(f"ALTER TABLE {quote_ident(output_table)} {idx_def}")
            except pymysql.err.OperationalError as exc:
                # 1061: Duplicate key name — 索引已存在，忽略
                if exc.args and exc.args[0] != 1061:
                    raise
    conn.commit()


def max_processed_source_row_id(conn, output_table: str, spec: SourceSpec) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COALESCE(MAX(source_row_id), 0) AS max_id
            FROM {quote_ident(output_table)}
            WHERE source_platform = %s AND source_table = %s
            """,
            (spec.platform, spec.table),
        )
        row = cur.fetchone() or {}
    return int(row.get("max_id") or 0)


def iter_source_rows(spec: SourceSpec, min_source_row_id: int = -1, limit: int = 0) -> Iterable[dict[str, Any]]:
    conn = connect_mysql(cursorclass=pymysql.cursors.SSDictCursor)
    try:
        sql = (
            f"SELECT {selected_columns(spec)} "
            f"FROM {quote_ident(spec.table)} "
            f"WHERE {quote_ident(spec.id_col)} > %s "
            f"AND comment_text IS NOT NULL AND TRIM(comment_text) <> '' "
            f"ORDER BY {quote_ident(spec.id_col)} ASC"
        )
        params: list[Any] = [min_source_row_id]
        if limit > 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                yield row
    finally:
        conn.close()


def source_context_for_row(spec: SourceSpec, row: dict[str, Any]) -> str:
    return " ".join(
        normalize_text(row.get(column))
        for column in (spec.title_col, spec.content_col, spec.keyword_col)
        if normalize_text(row.get(column))
    )


def build_output_row(run_id: str, spec: SourceSpec, row: dict[str, Any], signals: list[str], intents: list[str], score: int, mentions_brand: bool, mentions_condition: bool) -> dict[str, Any]:
    text = normalize_text(row.get("comment_text"))
    external_id = normalize_text(row.get(spec.external_id_col))
    source_record_key = external_id or f"row:{row.get(spec.id_col)}"
    condition = condition_metadata(text, source_context_for_row(spec, row))
    return {
        "run_id": run_id,
        "source_platform": spec.platform,
        "source_schema": get_mysql_config()["database"],
        "source_table": spec.table,
        "source_row_id": row.get(spec.id_col),
        "external_id": external_id or None,
        "source_record_key": source_record_key,
        "comment_text": row.get("comment_text") or "",
        "normalized_text": text,
        "intent_labels": "、".join(dict.fromkeys(intents)),
        "matched_signals": "、".join(dict.fromkeys(signals)),
        "choice_score": score,
        "mentions_brand": 1 if mentions_brand else 0,
        "mentions_condition": 1 if mentions_condition else 0,
        "condition_confidence": condition["confidence"],
        "condition_categories": condition["categories"],
        "condition_symptoms": condition["symptoms"],
        "condition_keywords": condition["keywords"],
        "source_title": row.get(spec.title_col),
        "source_content": row.get(spec.content_col),
        "source_like_count": row.get(spec.like_col),
        "source_comment_time": normalize_text(row.get(spec.time_col)) or None,
        "source_keyword": normalize_text(row.get(spec.keyword_col)) or None,
        "inserted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def insert_batch(conn, output_table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = f"""
        INSERT IGNORE INTO {quote_ident(output_table)} (
          run_id, source_platform, source_schema, source_table, source_row_id,
          external_id, source_record_key, comment_text, normalized_text, intent_labels, matched_signals,
          choice_score, mentions_brand, mentions_condition, condition_confidence,
          condition_categories, condition_symptoms, condition_keywords,
          source_title, source_content, source_like_count,
          source_comment_time, source_keyword, inserted_at
        )
        VALUES (
          %(run_id)s, %(source_platform)s, %(source_schema)s, %(source_table)s, %(source_row_id)s,
          %(external_id)s, %(source_record_key)s, %(comment_text)s, %(normalized_text)s, %(intent_labels)s, %(matched_signals)s,
          %(choice_score)s, %(mentions_brand)s, %(mentions_condition)s, %(condition_confidence)s,
          %(condition_categories)s, %(condition_symptoms)s, %(condition_keywords)s,
          %(source_title)s, %(source_content)s, %(source_like_count)s,
          %(source_comment_time)s, %(source_keyword)s, %(inserted_at)s
        )
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "source_platform",
        "source_row_id",
        "external_id",
        "intent_labels",
        "matched_signals",
        "choice_score",
        "mentions_brand",
        "mentions_condition",
        "condition_confidence",
        "condition_categories",
        "condition_symptoms",
        "condition_keywords",
        "comment_text",
        "source_title",
        "source_like_count",
        "source_comment_time",
        "source_keyword",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-table", default=OUTPUT_TABLE)
    parser.add_argument("--output-dir", default=str(ARTIFACT_ROOT))
    parser.add_argument("--limit", type=int, default=0, help="debug limit per source table; 0 means all new rows")
    parser.add_argument("--min-id", type=int, default=0, help="debug: override min source_row_id (skip rows with id <= this value)")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = "catfood_choice_comments_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    run_dir = Path(args.output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    output_conn = connect_mysql()
    try:
        ensure_output_table(output_conn, args.output_table)
        all_matches: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        summary: dict[str, dict[str, int]] = {}
        for table in SOURCE_TABLES:
            spec = SOURCE_SPECS[table]
            # 每次扫描全部源记录，通过 source_record_key 唯一键幂等写入。
            # 不能再用 MAX(source_row_id) 做水位，因为历史小红书数据大量 id=0。
            min_source_row_id = args.min_id if args.min_id > 0 else -1
            scanned = 0
            matched = 0
            for row in iter_source_rows(spec, min_source_row_id, args.limit):
                scanned += 1
                source_context = source_context_for_row(spec, row)
                keep, signals, intents, score, mentions_brand, mentions_condition = is_choice_comment(
                    row.get("comment_text"), source_context
                )
                if not keep:
                    continue
                matched += 1
                output_row = build_output_row(run_id, spec, row, signals, intents, score, mentions_brand, mentions_condition)
                pending.append(output_row)
                all_matches.append(output_row)
                if not args.dry_run and len(pending) >= BATCH_SIZE:
                    insert_batch(output_conn, args.output_table, pending)
                    pending.clear()
            summary[table] = {
                "min_source_row_id": min_source_row_id,
                "scanned": scanned,
                "matched": matched,
            }
        if not args.dry_run:
            insert_batch(output_conn, args.output_table, pending)

        csv_path = run_dir / "catfood_choice_comments.csv"
        write_csv(csv_path, all_matches)
        summary_payload = {
            "run_id": run_id,
            "source_tables": SOURCE_TABLES,
            "output_table": args.output_table,
            "matched_total": len(all_matches),
            "summary": summary,
            "dry_run": bool(args.dry_run),
            "csv": str(csv_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary_payload, ensure_ascii=False, indent=2))
    finally:
        output_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
