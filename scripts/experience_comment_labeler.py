#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Experience 评论细粒度打标脚本。

对 catfood_choice_comments_filtered_v2 中 intent_labels 含 Experience 的评论打标：
- primary_symptom：主病症（Experience 里最核心的问题）
- secondary_symptom：次病症（主病症之外，文本里出现的其他问题）
- secondary_onset：次病症出现阶段（使用前已有 / 使用后出现）
- secondary_outcome：次病症结果（改善 / 未改善 / 加重 / 持续）

输出表：catfood_experience_comment_labels
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402

DEFAULT_SOURCE_TABLE = "catfood_choice_comments_filtered_v2"
DEFAULT_TARGET_TABLE = "catfood_experience_comment_labels"
LABEL_VERSION = "experience_comment_rules_v1"
DB_BATCH_SIZE = 1000


# === 症状词表（复用 SYMPTOM_KEYWORDS 结构） ===
SYMPTOM_KEYWORDS = {
    "消化系统问题": {
        "软便/拉稀": [
            "软便", "便软", "拉稀", "腹泻", "拉肚子", "不成形",
            "稀便", "糊状便", "便便稀", "便便不成形", "反复拉",
            "便血", "肠胃不好", "肠胃不适", "肚子不舒服",
            "窜稀", "拉肚", "拉水", "拉血", "末端软", "末端带血",
        ],
        "拉屎臭": [
            "屎臭", "拉屎臭", "便便臭", "臭便", "巨臭",
            "特别臭", "粑粑臭", "臭臭", "拉的臭", "💩巨臭", "屎巨臭",
            "便臭", "粑粑巨臭", "很臭", "超臭",
        ],
        "呕吐": [
            "呕吐", "吐了", "吐粮", "吐黄水", "吐猫粮",
            "吃完吐", "反胃", "吐白沫", "吐毛球", "吐白色泡沫",
            "吐的是", "又吐了", "吃了吐", "喂了吐",
            "都吐", "吃吐", "一吃就吐", "吃了就吐",
            "换粮吐", "换了吐", "开始吐", "开始呕吐",
            "吐出来", "吃下去就吐", "一喂就吐",
        ],
        "便秘/干硬便": [
            "便秘", "拉不出来", "小疙瘩", "干便", "硬便",
            "一粒一粒", "不是一长条", "颗粒便", "羊屎蛋", "羊屎蛋儿",
            "干巴巴", "大便干", "便便干", "拉羊屎",
        ],
    },
    "皮肤毛发问题": {
        "黑下巴": [
            "黑下巴", "下巴黑", "毛囊炎", "粉刺", "油下巴",
            "下巴脏", "下巴结痂", "下巴有黑点",
        ],
        "油尾巴": [
            "油尾巴", "尾巴油", "种马尾",
        ],
        "掉毛": [
            "掉毛", "脱毛", "掉毛严重", "毛变少", "秃",
            "疯狂掉毛", "掉毛厉害",
        ],
        "瘙痒/过敏": [
            "过敏", "瘙痒", "抓挠", "红疹", "皮肤红",
            "皮屑", "挠", "痒", "一直挠", "总挠", "皮肤敏感",
            "挠耳朵", "舔毛过度", "咬毛", "舔秃",
        ],
    },
    "眼部问题": {
        "泪痕": [
            "泪痕", "眼屎", "眼睛分泌物", "眼周发红",
            "流泪", "眼泪多", "眼泪", "抠眼屎", "眼泪痕",
        ],
    },
    "适口性问题": {
        "不爱吃": [
            "不爱吃", "不吃", "挑食", "闻了就走", "适口性差",
            "不肯吃", "吃得少", "一口不吃", "闻了不吃",
            "吃两口就走", "不碰", "拒食",
        ],
    },
    "泌尿系统问题": {
        "尿结晶/尿路问题": [
            "泌尿", "尿闭", "尿血", "血尿", "尿结晶", "结晶尿", "尿路结晶",
            "尿结石", "膀胱结石", "肾结石", "膀胱炎", "尿路感染", "尿频",
            "尿不出", "尿不出来", "排尿困难", "尿少", "乱尿", "蹲猫砂盆",
            "尿路感染", "结晶",
        ],
    },
    "体重与代谢问题": {
        "肥胖/体重管理": [
            "肥胖", "超重", "体重超标", "绝育后发胖", "绝育后长胖", "减肥",
            "控制体重", "体重管理", "易胖", "太胖", "虚胖", "长胖了",
            "越来越胖", "胖了",
        ],
        "增重/长肉": [
            "长肉", "长胖", "增重", "增肥", "养胖", "喂胖", "吃胖", "变胖",
            "长体重", "体重增长", "体重增加", "涨体重", "发腮", "爆腮", "长腮",
            "长肉肉", "养肉肉", "体重涨了", "涨肉", "长了肉",
            "体重涨", "胖了两斤", "胖了几斤", "胖了二斤", "长了两斤", "长了几斤",
            "涨了两斤", "涨了几斤",
        ],
        "消瘦/不长肉": [
            "不长肉", "长不胖", "太瘦", "偏瘦", "消瘦", "体重下降", "掉秤",
            "怎么都不胖", "一直不胖", "瘦了",
        ],
    },
    "口腔问题": {
        "口臭/口腔问题": [
            "口臭", "嘴臭", "牙龈红肿", "牙龈炎", "口炎", "牙结石", "流口水",
            "嘴巴臭", "口气重", "牙齿黄",
        ],
    },
}

# 使用动作词：标识"使用某产品"的时间锚点
USE_ACTION_PATTERNS = [
    r"吃了", r"喂了", r"在吃", r"我家吃", r"一直吃", r"试了", r"试吃过", r"试过",
    r"吃过", r"买过", r"入了", r"入手", r"吃完", r"喂着", r"吃着", r"我家喂",
    r"从小喂", r"喂到大", r"从小吃", r"一直喂", r"一直在喂", r"一直在吃",
    r"在喂", r"买的是", r"买的", r"我家是吃", r"我家在喂", r"换了", r"换成",
    r"换粮", r"换到", r"吃这个", r"吃这款", r"喂这个", r"喂这款", r"这款吃",
    r"这款粮", r"这个粮", r"吃了[一二三四五六七八九十\d]+[天周月年]",
    r"喂了[一二三四五六七八九十\d]+[天周月年]", r"吃了有?[一二三四五六七八九十\d]+[天周月年]",
    r"吃\S{0,6}(?:之后|以后|后)", r"喂\S{0,6}(?:之后|以后|后)",
    r"换\S{0,6}(?:之后|以后|后)",
]

# 使用前已有：标识"之前就存在问题"
PRE_EXISTING_PATTERNS = [
    r"以前", r"之前", r"本来", r"原来就", r"一直有", r"一直就", r"之前就",
    r"之前一直", r"以前一直", r"老毛病", r"天生", r"本来就", r"早就",
    r"换粮前", r"吃这个之前", r"喂之前", r"以前就", r"之前老", r"之前总",
    r"之前经常", r"本来就是", r"一直都是",
]

# 使用后出现：标识"换了/吃了之后才出现"
POST_USE_PATTERNS = [
    r"吃了.{0,20}(?:之后|以后|后).{0,10}(?:开始|就|出现|发现|变成)",
    r"喂了.{0,20}(?:之后|以后|后).{0,10}(?:开始|就|出现|发现|变成)",
    r"换了.{0,20}(?:之后|以后|后).{0,10}(?:开始|就|出现|发现|变成)",
    r"吃了.{0,30}(?:开始|就).{0,10}(?:软便|拉稀|吐|拉|便)",
    r"吃完.{0,10}(?:就|开始)",
    r"吃这个.{0,10}(?:之后|以后|开始)",
]

# 改善词
IMPROVE_PATTERNS = [
    r"不软便", r"不拉", r"不吐", r"不拉稀", r"没有软便", r"没软便",
    r"好了", r"好很多", r"好了很多", r"改善", r"缓解", r"解决", r"消失",
    r"没了", r"不见了", r"不黑下巴", r"没黑下巴", r"没有黑下巴",
    r"没有泪痕", r"没泪痕", r"泪痕没了", r"泪痕好了", r"眼屎少",
    r"不臭了", r"便便正常", r"粑粑正常", r"正常了", r"不拉了", r"不吐了",
    r"不便秘了", r"不怎么.{0,5}了", r"好多了", r"好转", r"明显好转",
    r"变少了", r"减轻了", r"减轻", r"减少了", r"光滑了", r"顺滑了",
    r"毛很滑", r"毛发顺滑", r"长肉了", r"长胖了", r"长肉肉了",
    r"爱吃了", r"肯吃了", r"吃的香", r"吃得香", r"不挑了",
    r"没有.{0,6}问题", r"没问题", r"没什么问题", r"挺ok", r"挺OK",
    r"粑粑什么都正常", r"便便什么都正常", r"一切正常", r"都正常",
    r"没有出现", r"没出现", r"没发现", r"没发现过",
]

# 加重词
WORSEN_PATTERNS = [
    r"更严重", r"更厉害", r"更.{0,4}了", r"越来越.{0,4}", r"加重",
    r"严重了", r"厉害了", r"变严重", r"更软", r"更稀", r"更臭",
    r"拉的更", r"吐的更", r"越来越严重",
]

# 持续/未改善词
PERSIST_PATTERNS = [
    r"还是", r"依然", r"依旧", r"仍然", r"还是有", r"还是软便",
    r"还是拉", r"还是吐", r"还是黑下巴", r"还是泪痕", r"还是不爱吃",
    r"还是不", r"还是没", r"还是会", r"依然有", r"没改善", r"没缓解",
    r"没好", r"没好转", r"没变化", r"还是那样", r"还是老样子",
    r"照样", r"照旧",
]


def normalize_text(text):
    if text is None:
        return ""
    try:
        if text != text:
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(text)).strip()


def unique(items):
    return list(dict.fromkeys(items))


def re_search(pattern, text, flags=re.IGNORECASE):
    return bool(re.search(pattern, text, flags=flags))


def re_finditer_positions(pattern, text, flags=re.IGNORECASE):
    """返回所有匹配的 (start, end, matched_text)。"""
    results = []
    for m in re.finditer(pattern, text, flags=flags):
        results.append((m.start(), m.end(), m.group(0)))
    return results


def build_symptom_index():
    """构建症状词索引：每个症状二级类目 → 正则 pattern + 位置匹配函数。

    Returns:
        list of (primary, secondary, pattern_str, compiled_regex)
        按关键词长度降序排列（优先匹配长词）
    """
    entries = []
    for primary, sub_map in SYMPTOM_KEYWORDS.items():
        for secondary, keywords in sub_map.items():
            # 按长度降序排列，避免短词先匹配
            sorted_keywords = sorted(keywords, key=len, reverse=True)
            pattern_str = "|".join(re.escape(kw) for kw in sorted_keywords)
            compiled = re.compile(pattern_str, flags=re.IGNORECASE)
            entries.append((primary, secondary, pattern_str, compiled))
    return entries


SYMPTOM_INDEX = build_symptom_index()


def find_symptom_mentions(text):
    """在文本中查找所有症状提及，返回带位置信息的列表。

    Returns:
        list of {primary, secondary, keyword, start, end}
    """
    if not text:
        return []
    mentions = []
    seen_spans = set()
    for primary, secondary, _, compiled in SYMPTOM_INDEX:
        for m in compiled.finditer(text):
            span = (m.start(), m.end())
            # 避免重叠：如果已被更长词覆盖则跳过
            overlap = False
            for s, e in seen_spans:
                if not (m.end() <= s or m.start() >= e):
                    overlap = True
                    break
            if overlap:
                continue
            seen_spans.add(span)
            mentions.append({
                "primary": primary,
                "secondary": secondary,
                "keyword": m.group(0),
                "start": m.start(),
                "end": m.end(),
                "negated": is_negated(m.start(), m.end(), text),
            })
    mentions.sort(key=lambda x: x["start"])
    return mentions


NEGATION_BEFORE_PATTERNS = [
    r"不\s*", r"没\s*", r"没有\s*", r"无\s*", r"不会\s*",
    r"没发现\s*", r"没出现\s*", r"没有出现\s*", r"没发现过\s*",
    r"改善\s*", r"缓解\s*", r"消除\s*", r"治好了\s*", r"治好\s*",
]


def is_negated(start, end, text, window=10):
    """判断症状是否被否定修饰——即该症状从未发生/不存在（如"不软便""没黑下巴""没有出现呕吐"）。

    注意：症状+好了/没了/改善（如"软便好了"）不算否定，因为症状之前存在过，只是改善了。
    """
    pre = text[max(0, start - window):start]
    # 前面有否定词（没/无/不/没有/没出现/没发现/没有出现/没有发现）
    negation_patterns = [
        r"不\s*$",
        r"没(有|出现|发现过?)?\s*$",
        r"没有(出现|发现过?)?\s*$",
        r"无\s*$",
        r"不会\s*$",
    ]
    for pat in negation_patterns:
        if re.search(pat, pre):
            return True
    return False


def find_use_action_positions(text):
    """找到所有使用动作词的位置。"""
    positions = []
    for pat in USE_ACTION_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            positions.append((m.start(), m.end(), m.group(0)))
    positions.sort(key=lambda x: x[0])
    return positions


def find_effect_word_positions(text):
    """找到改善/加重/持续词的位置。"""
    improves = []
    worsens = []
    persists = []
    for pat in IMPROVE_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            improves.append((m.start(), m.end(), m.group(0)))
    for pat in WORSEN_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            worsens.append((m.start(), m.end(), m.group(0)))
    for pat in PERSIST_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            persists.append((m.start(), m.end(), m.group(0)))
    improves.sort(key=lambda x: x[0])
    worsens.sort(key=lambda x: x[0])
    persists.sort(key=lambda x: x[0])
    return improves, worsens, persists


def classify_outcome_for_symptom(symptom_pos, improves, worsens, persists, text):
    """判断某个症状（给定位置）的结果：改善/加重/持续/未改善。

    逻辑：在症状位置前后 ±30 字符窗口内找效果词；
    如果有"不+症状"直接算改善（如"不软便了"）；
    否则取距离症状最近的效果词决定结果。
    """
    s, e = symptom_pos
    text_len = len(text)
    symptom_center = (s + e) / 2
    window_start = max(0, s - 30)
    window_end = min(text_len, e + 30)

    # 检查"不+症状"模式（如"不软便""没有软便""没软便"）
    pre_text = text[max(0, s - 5):s]
    if re.search(r"(不|没|没有|无)\s*$", pre_text):
        return "改善"
    post_text = text[e:min(text_len, e + 3)]
    if re.search(r"^(好了|好|没了|消失|改善|缓解)", post_text):
        return "改善"

    # 找窗口内所有效果词，按到症状中心的距离排序，取最近的
    candidates = []
    for w in worsens:
        if window_start <= w[0] <= window_end or window_start <= w[1] <= window_end:
            w_center = (w[0] + w[1]) / 2
            candidates.append((abs(w_center - symptom_center), "加重"))
    for w in improves:
        if window_start <= w[0] <= window_end or window_start <= w[1] <= window_end:
            w_center = (w[0] + w[1]) / 2
            # 改善词必须出现在症状之后（避免把前一个症状的改善词算到后一个症状上）
            # 例外："不+症状"已经在上面处理了
            if w[0] >= s - 5:
                candidates.append((abs(w_center - symptom_center), "改善"))
    for w in persists:
        if window_start <= w[0] <= window_end or window_start <= w[1] <= window_end:
            w_center = (w[0] + w[1]) / 2
            candidates.append((abs(w_center - symptom_center), "持续"))

    if not candidates:
        return "未改善"
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def classify_onset_for_symptom(symptom_pos, use_actions, text):
    """判断次病症出现阶段：使用前已有 / 使用后出现 / 未知。"""
    s, e = symptom_pos
    window_before = text[max(0, s - 30):s]
    window_after = text[e:min(len(text), e + 30)]

    # 优先检测"使用后出现"强信号：症状紧接在"之后/以后/后"后面，或症状跟在使用动作+结果词后面
    # 模式：吃了/喂了/换了...之后/以后/后 出现/开始/就/有/出...症状
    post_use_pattern = re.compile(
        r"(?:之后|以后|后|开始|然后|结果|就).{0,10}$",
        flags=re.IGNORECASE,
    )
    if post_use_pattern.search(window_before):
        # 检查前面是否确实有使用动作
        has_use_before = any(ue <= s for _, ue, _ in use_actions)
        if has_use_before:
            return "使用后出现"

    # "也出来了/也有了/长出/新增"等强使用后信号
    if re.search(r"(也|又|新|开始|出现|长出|出来).{0,5}(了|有)", window_after, flags=re.IGNORECASE):
        has_use_before = any(ue <= s for _, ue, _ in use_actions)
        if has_use_before:
            return "使用后出现"

    # 使用后出现：症状在使用动作之后，且附近（<30字）
    nearest_use_before = None
    for us, ue, _ in use_actions:
        if ue <= s:
            nearest_use_before = (us, ue)
        else:
            break

    if nearest_use_before and s - nearest_use_before[1] < 30:
        # 症状紧接在使用动作后面，且没有"之前/以前"等词把症状隔开
        between_text = text[nearest_use_before[1]:s]
        if not re.search(r"(之前|以前|本来|原来|一直就|早就)", between_text, flags=re.IGNORECASE):
            return "使用后出现"

    # 使用前已有：在症状紧邻的前面（20字内）有"之前/本来/以前就"等，且中间没有"之后/以后"转折
    pre_20 = text[max(0, s - 20):s]
    has_pre = False
    for pat in PRE_EXISTING_PATTERNS:
        if re.search(pat, pre_20, flags=re.IGNORECASE):
            # 检查"之前/以前"和症状之间是否有"之后/后"等转折
            pre_match = re.search(pat, pre_20, flags=re.IGNORECASE)
            if pre_match:
                after_pre = pre_20[pre_match.end():]
                if not re.search(r"(之后|以后|后|换了|换成)", after_pre, flags=re.IGNORECASE):
                    has_pre = True
                    break
    if has_pre:
        return "使用前已有"

    # 远距离检查：前面40字有"之前/以前"且没有使用动作隔开
    pre_40 = text[max(0, s - 40):s]
    if nearest_use_before is None:
        # 前面没有使用动作，有"之前"则是使用前已有
        for pat in PRE_EXISTING_PATTERNS:
            if re.search(pat, pre_40, flags=re.IGNORECASE):
                return "使用前已有"

    return "未知"


def determine_primary_symptom(mentions, use_actions, improves, worsens, text):
    """确定主病症。

    策略（优先级）：
    1. 被效果词（改善/加重）直接修饰的症状
    2. 最靠近第一个使用动作词的症状
    3. 文本中最先出现的症状
    """
    if not mentions:
        return None, []

    # 1. 找被效果词修饰的症状
    effect_symptoms = []
    for m in mentions:
        outcome = classify_outcome_for_symptom(
            (m["start"], m["end"]), improves, worsens, [], text
        )
        if outcome in ("改善", "加重"):
            effect_symptoms.append(m)

    if effect_symptoms:
        # 如果有多个，取最靠近第一个使用动作的
        if use_actions:
            first_use = use_actions[0][0]
            effect_symptoms.sort(key=lambda x: abs(x["start"] - first_use))
        primary = effect_symptoms[0]
    elif use_actions:
        # 2. 取最靠近第一个使用动作的症状
        first_use = use_actions[0][0]
        mentions_sorted = sorted(mentions, key=lambda x: abs(x["start"] - first_use))
        primary = mentions_sorted[0]
    else:
        # 3. 取第一个症状
        primary = mentions[0]

    # 剩余的是次病症
    secondary_mentions = [m for m in mentions if m is not primary]
    return primary, secondary_mentions


def label_experience_comment(text):
    """对一条 Experience 评论打标。

    Returns:
        dict with primary_symptom, secondary_symptom, secondary_onset,
        secondary_outcome, matched_keywords
    """
    text = normalize_text(text)
    if not text:
        return {
            "primary_symptom": "",
            "primary_symptom_primary": "",
            "secondary_symptom": "",
            "secondary_onset": "",
            "secondary_outcome": "",
            "matched_keywords": "",
        }

    mentions = find_symptom_mentions(text)
    # 过滤被否定的症状（如"没有黑下巴""不软便"）
    mentions = [m for m in mentions if not m.get("negated", False)]
    use_actions = find_use_action_positions(text)
    improves, worsens, persists = find_effect_word_positions(text)

    # 收集所有命中的关键词
    all_keywords = []
    for m in mentions:
        all_keywords.append(f"{m['primary']}>{m['secondary']}:{m['keyword']}")
    for w in improves:
        all_keywords.append(f"效果:改善:{w[2]}")
    for w in worsens:
        all_keywords.append(f"效果:加重:{w[2]}")
    for w in persists:
        all_keywords.append(f"效果:持续:{w[2]}")

    if not mentions:
        return {
            "primary_symptom": "",
            "primary_symptom_primary": "",
            "secondary_symptom": "",
            "secondary_onset": "",
            "secondary_outcome": "",
            "matched_keywords": " | ".join(unique(all_keywords)),
        }

    primary, secondary_mentions = determine_primary_symptom(
        mentions, use_actions, improves, worsens, text
    )

    primary_symptom = primary["secondary"]
    primary_symptom_primary = primary["primary"]

    # 处理次病症（可能有多个）：按 secondary 名称去重，排除主病症
    secondary_mentions_dedup = []
    seen_secondary = {primary["secondary"]}
    for sm in secondary_mentions:
        if sm["secondary"] not in seen_secondary:
            seen_secondary.add(sm["secondary"])
            secondary_mentions_dedup.append(sm)
    secondary_mentions = secondary_mentions_dedup

    secondary_symptoms = []
    secondary_onsets = []
    secondary_outcomes = []
    for sm in secondary_mentions:
        secondary_symptoms.append(sm["secondary"])
        onset = classify_onset_for_symptom((sm["start"], sm["end"]), use_actions, text)
        secondary_onsets.append(onset)
        outcome = classify_outcome_for_symptom(
            (sm["start"], sm["end"]), improves, worsens, persists, text
        )
        secondary_outcomes.append(outcome)

    # 去重
    secondary_symptoms = unique(secondary_symptoms)
    # 对应 onset/outcome 按症状去重（取第一个匹配的）
    onset_map = {}
    outcome_map = {}
    for sm, on, oc in zip(
        [m["secondary"] for m in secondary_mentions],
        secondary_onsets,
        secondary_outcomes,
    ):
        if sm not in onset_map:
            onset_map[sm] = on
        if sm not in outcome_map:
            outcome_map[sm] = oc

    secondary_symptom_str = " | ".join(secondary_symptoms)
    secondary_onset_str = " | ".join(f"{s}:{onset_map.get(s, '未知')}" for s in secondary_symptoms)
    secondary_outcome_str = " | ".join(f"{s}:{outcome_map.get(s, '未改善')}" for s in secondary_symptoms)

    return {
        "primary_symptom": primary_symptom,
        "primary_symptom_primary": primary_symptom_primary,
        "secondary_symptom": secondary_symptom_str,
        "secondary_onset": secondary_onset_str,
        "secondary_outcome": secondary_outcome_str,
        "matched_keywords": " | ".join(unique(all_keywords)),
    }


def quote_ident(name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"不安全的表名: {name}")
    return f"`{name}`"


def connect_mysql(cursorclass=pymysql.cursors.DictCursor, autocommit=False):
    return pymysql.connect(
        **get_mysql_config(), cursorclass=cursorclass, autocommit=autocommit
    )


def ensure_target_table(conn, target_table):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(target_table)} (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              source_comment_id BIGINT NOT NULL,
              source_platform VARCHAR(32) NOT NULL,
              source_table VARCHAR(64) NOT NULL,
              source_record_key VARCHAR(255) NOT NULL,
              external_id VARCHAR(255) NULL,
              comment_text LONGTEXT NOT NULL,
              intent_labels VARCHAR(255) NOT NULL,
              primary_symptom_primary VARCHAR(64) NOT NULL DEFAULT '' COMMENT '主病症一级类目',
              primary_symptom VARCHAR(128) NOT NULL DEFAULT '' COMMENT '主病症二级类目',
              secondary_symptom TEXT COMMENT '次病症（多值 | 分隔）',
              secondary_onset TEXT COMMENT '次病症出现阶段（格式：症状:阶段，| 分隔）',
              secondary_outcome TEXT COMMENT '次病症结果（格式：症状:结果，| 分隔）',
              exp_matched_keywords TEXT COMMENT '命中关键词',
              exp_label_json JSON NOT NULL,
              exp_detail_labeled TINYINT(1) NOT NULL DEFAULT 0,
              label_version VARCHAR(64) NOT NULL,
              labeled_at DATETIME NOT NULL,
              UNIQUE KEY uq_source_label (source_comment_id, label_version),
              KEY idx_source_record (source_platform, source_table, source_record_key),
              KEY idx_primary_symptom (primary_symptom),
              KEY idx_exp_detail (exp_detail_labeled),
              KEY idx_label_version (label_version)
            ) DEFAULT CHARSET=utf8mb4 COMMENT='Experience 评论细粒度打标'
            """
        )
    conn.commit()


def iter_source_rows(source_table, *, all_rows=False, limit=0):
    conn = connect_mysql(cursorclass=pymysql.cursors.SSDictCursor)
    try:
        sql = f"""
            SELECT id, source_platform, source_table, source_record_key,
                   external_id, comment_text, intent_labels
            FROM {quote_ident(source_table)}
            WHERE comment_text IS NOT NULL AND TRIM(comment_text) <> ''
        """
        params = []
        if not all_rows:
            sql += " AND FIND_IN_SET('Experience', REPLACE(intent_labels, '、', ',')) > 0"
        sql += " ORDER BY id ASC"
        if limit > 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                yield row
    finally:
        conn.close()


def build_db_row(row):
    result = label_experience_comment(row.get("comment_text"))
    return {
        "source_comment_id": row.get("id"),
        "source_platform": normalize_text(row.get("source_platform")),
        "source_table": normalize_text(row.get("source_table")),
        "source_record_key": normalize_text(row.get("source_record_key")),
        "external_id": normalize_text(row.get("external_id")) or None,
        "comment_text": normalize_text(row.get("comment_text")),
        "intent_labels": normalize_text(row.get("intent_labels")),
        "primary_symptom_primary": result["primary_symptom_primary"],
        "primary_symptom": result["primary_symptom"],
        "secondary_symptom": result["secondary_symptom"],
        "secondary_onset": result["secondary_onset"],
        "secondary_outcome": result["secondary_outcome"],
        "exp_matched_keywords": result["matched_keywords"],
        "exp_label_json": json.dumps(result, ensure_ascii=False),
        "exp_detail_labeled": int(bool(result["primary_symptom"])),
        "label_version": LABEL_VERSION,
        "labeled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def upsert_batch(conn, target_table, rows):
    if not rows:
        return 0
    sql = f"""
        INSERT INTO {quote_ident(target_table)} (
          source_comment_id, source_platform, source_table, source_record_key,
          external_id, comment_text, intent_labels, primary_symptom_primary,
          primary_symptom, secondary_symptom, secondary_onset, secondary_outcome,
          exp_matched_keywords, exp_label_json, exp_detail_labeled,
          label_version, labeled_at
        ) VALUES (
          %(source_comment_id)s, %(source_platform)s, %(source_table)s, %(source_record_key)s,
          %(external_id)s, %(comment_text)s, %(intent_labels)s, %(primary_symptom_primary)s,
          %(primary_symptom)s, %(secondary_symptom)s, %(secondary_onset)s, %(secondary_outcome)s,
          %(exp_matched_keywords)s, %(exp_label_json)s, %(exp_detail_labeled)s,
          %(label_version)s, %(labeled_at)s
        )
        ON DUPLICATE KEY UPDATE
          comment_text=VALUES(comment_text), intent_labels=VALUES(intent_labels),
          primary_symptom_primary=VALUES(primary_symptom_primary),
          primary_symptom=VALUES(primary_symptom),
          secondary_symptom=VALUES(secondary_symptom),
          secondary_onset=VALUES(secondary_onset),
          secondary_outcome=VALUES(secondary_outcome),
          exp_matched_keywords=VALUES(exp_matched_keywords),
          exp_label_json=VALUES(exp_label_json),
          exp_detail_labeled=VALUES(exp_detail_labeled), labeled_at=VALUES(labeled_at)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


def run_database(source_table, target_table, *, all_rows=False, limit=0, dry_run=False):
    output_conn = connect_mysql()
    scanned = labeled = detailed = 0
    primary_counts = defaultdict(int)
    pending = []
    try:
        if not dry_run:
            ensure_target_table(output_conn, target_table)
        for row in iter_source_rows(source_table, all_rows=all_rows, limit=limit):
            scanned += 1
            output_row = build_db_row(row)
            labeled += 1
            detailed += output_row["exp_detail_labeled"]
            if output_row["primary_symptom"]:
                primary_counts[output_row["primary_symptom"]] += 1
            if dry_run:
                continue
            pending.append(output_row)
            if len(pending) >= DB_BATCH_SIZE:
                upsert_batch(output_conn, target_table, pending)
                pending.clear()
        if not dry_run:
            upsert_batch(output_conn, target_table, pending)
    finally:
        output_conn.close()

    summary = {
        "mode": "database",
        "source_table": source_table,
        "target_table": target_table,
        "label_version": LABEL_VERSION,
        "all_rows": all_rows,
        "dry_run": dry_run,
        "scanned": scanned,
        "labeled": labeled,
        "exp_detail_labeled": detailed,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if primary_counts:
        print("\n主病症分布:")
        for sym, cnt in sorted(primary_counts.items(), key=lambda x: -x[1]):
            print(f"  {sym}: {cnt}")

    return summary


# 文件模式（可选）
def run_file(args):
    import pandas as pd
    input_path = Path(args.input)
    if input_path.suffix.lower() == ".csv":
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                df = pd.read_csv(input_path, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("CSV 编码无法识别")
    elif input_path.suffix in (".xlsx", ".xls"):
        df = pd.read_excel(input_path)
    else:
        raise ValueError("仅支持 csv/xlsx/xls")

    if args.text_col not in df.columns:
        raise ValueError(f"评论字段不存在: {args.text_col}")

    results = []
    for _, row in df.iterrows():
        text = row.get(args.text_col, "")
        result = label_experience_comment(text)
        results.append(result)

    for col in ["primary_symptom", "primary_symptom_primary", "secondary_symptom",
                 "secondary_onset", "secondary_outcome", "exp_matched_keywords"]:
        df[col] = [r[col] for r in results]
    df["exp_detail_labeled"] = [int(bool(r["primary_symptom"])) for r in results]
    df["label_version"] = LABEL_VERSION

    output_path = input_path.with_name(input_path.stem + "_labeled.csv")
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"输出文件: {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Experience 评论细粒度打标")
    parser.add_argument("--database", action="store_true", help="数据库模式")
    parser.add_argument("--source-table", default=DEFAULT_SOURCE_TABLE)
    parser.add_argument("--target-table", default=DEFAULT_TARGET_TABLE)
    parser.add_argument("--all-rows", action="store_true", help="处理全表而非仅 Experience 行")
    parser.add_argument("--limit", type=int, default=0, help="限制处理行数")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--input", help="文件模式输入路径")
    parser.add_argument("--text-col", default="comment_text", help="文件模式评论列名")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.database or not args.input:
        run_database(
            args.source_table, args.target_table,
            all_rows=args.all_rows, limit=args.limit, dry_run=args.dry_run,
        )
    else:
        run_file(args)


if __name__ == "__main__":
    main()
