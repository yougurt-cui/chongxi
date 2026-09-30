"""语言层（v2 槽位组版 + 真实库产品识别）。

职责严格限制为两个方向的「语言 ⇄ 结构」转换：

1. `extract_user_info`         自然语言  → 槽位 JSON
2. `generate_response_from_plan` 结构化计划 → 自然语言

Pattern 分流、槽位完整性、Tool 调用条件全部由 Python 控制，不交给大模型。

比 v2 增强的点
--------------
- 支持中文数字（「两天」「十二天」「八成」）；
- 修正 v2 里 `(\\d+)天` 的误判：先把「软便X天」剥掉，再判断换粮天数，
  避免「软便3天了，换了五天粮」把 change_days 读成 3；
- 产品名不再依赖硬编码名单：优先用触发词（换成 / 原来吃 / 现在喂 …）抽取，
  再用 `tools.DEMO_PRODUCT_ALIAS`（A粮/B粮/C粮 → 真实产品）兜底。
"""

import json
import re

from .tools import DEMO_PRODUCT_ALIAS

MOCK_LLM = True

# 槽位组（与 pipeline.new_state 的 slots 一一对应）
SLOT_KEYS = (
    "recent_diet_change", "change_type", "old_product", "new_product",
    "change_ratio", "change_days",
    "soft_stool_days",
    "vomiting", "blood_in_stool", "appetite", "energy",
)

# v2 README 用例里的产品别名（真实库里映射到具体产品）
_ALIAS_NAMES = sorted(DEMO_PRODUCT_ALIAS.keys(), key=len, reverse=True)

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_CN_NUM = r"[0-9一二两三四五六七八九十]+"


def _cn_to_int(value):
    """把「两 / 十二 / 二十三 / 23」统一成 int，解析失败返回 None。"""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value in _CN_DIGITS:
        return _CN_DIGITS[value]
    m = re.fullmatch(r"十([一二两三四五六七八九])", value)
    if m:
        return 10 + _CN_DIGITS[m.group(1)]
    m = re.fullmatch(r"([一二两三四五六七八九])十([一二两三四五六七八九]?)", value)
    if m:
        return _CN_DIGITS[m.group(1)] * 10 + (_CN_DIGITS[m.group(2)] if m.group(2) else 0)
    return None


def call_real_llm(system_prompt: str, user_prompt: str) -> str:
    """
    TODO：在这里接千问 / OpenAI / 本地模型，要求返回 JSON 字符串。
    当前 Mock 模式不会走到这里。
    """
    raise NotImplementedError("请在 call_real_llm() 中接入真实大模型")


# =========================================================
# 1. 抽取
# =========================================================
def extract_user_info(user_input: str, state: dict) -> dict:
    """用户自然语言 → 槽位。返回只含「本轮新得到」的字段。"""
    if not MOCK_LLM:
        return json.loads(call_real_llm(_EXTRACT_SYSTEM_PROMPT, json.dumps({
            "user_input": user_input,
            "current_slots": (state or {}).get("slots", {}),
            "pattern": (state or {}).get("pattern"),
        }, ensure_ascii=False)))

    text = (user_input or "").strip()
    result = {}

    # ---------------- 近期饮食变化组 ----------------
    if any(x in text for x in (
        "换粮", "换了粮", "换新粮", "换了个粮", "换到", "换成", "改喂", "改吃",
        "加罐头", "加了罐头", "罐头", "加零食", "加了零食", "零食", "新食物",
        "新的东西", "换了", "转粮",
    )):
        result["recent_diet_change"] = True

    if any(x in text for x in (
        "没有换", "没换", "没有加", "没加", "没变化", "没有变化",
        "一直吃", "还是原来", "没变过",
    )):
        result["recent_diet_change"] = False

    if "罐头" in text or "湿粮" in text:
        result["change_type"] = "wet_food_added"
    elif "零食" in text:
        result["change_type"] = "snack_added"
    elif any(x in text for x in ("换粮", "换了粮", "换新粮", "换到", "换成", "改喂", "改吃", "转粮")):
        result["change_type"] = "main_food_change"

    # 产品名：先按「原来X，现在Y」成对抽
    m = re.search(r"(?:原来|原本|以前|之前)(?:吃|喂|是)?([^，。,；;、\s]+?)"
                  r"[^，。,；;、]*?(?:现在|换成|换到|改成|改吃|改喂)([^，。,；;、\s]+)", text)
    if m:
        result["old_product"] = _clean_product(m.group(1))
        result["new_product"] = _clean_product(m.group(2))
        result["recent_diet_change"] = True
        result["change_type"] = result.get("change_type") or "main_food_change"
    else:
        old = re.search(r"(?:原来|原本|以前|之前)(?:一直)?(?:吃|喂|是)?\s*([^，。,；;、\s]+)", text)
        if old:
            result["old_product"] = _clean_product(old.group(1))
        new = re.search(r"(?:换成|换到|改吃|改喂|现在吃|现在喂|新粮是|新粮换成|开始吃)\s*([^，。,；;、\s]+)", text)
        if new:
            result["new_product"] = _clean_product(new.group(1))
            result["recent_diet_change"] = True

    # 昵称兜底（A粮 / B粮 / C粮 …）
    for name in _ALIAS_NAMES:
        if name in text:
            if any(k in text for k in ("原来", "以前", "之前", "原本")):
                result.setdefault("old_product", name)
            else:
                result.setdefault("new_product", name)

    # ---------------- 比例 ----------------
    m = re.search(rf"({_CN_NUM})\s*成", text)
    if m:
        n = _cn_to_int(m.group(1))
        if n is not None and 0 <= n <= 10:
            result["change_ratio"] = n / 10.0
    if "一半" in text:
        result["change_ratio"] = 0.5
    m = re.search(r"(\d{1,3})\s*%", text)
    if m:
        pct = int(m.group(1))
        if 0 <= pct <= 100:
            result["change_ratio"] = pct / 100.0

    # ---------------- 软便持续天数（先抽，避免被换粮天数误用）----------------
    for pattern in (
        rf"软便\D{{0,4}}?({_CN_NUM})\s*天",
        rf"({_CN_NUM})\s*天\D{{0,4}}?软便",
        rf"拉稀\D{{0,4}}?({_CN_NUM})\s*天",
    ):
        m = re.search(pattern, text)
        if m:
            n = _cn_to_int(m.group(1))
            if n is not None:
                result["soft_stool_days"] = n
            break
    if "soft_stool_days" not in result:
        if re.search(r"软便|拉稀|稀便", text):
            if "昨天" in text:
                result["soft_stool_days"] = 1
            elif "前天" in text:
                result["soft_stool_days"] = 2

    # ---------------- 换粮天数（必须有「换 / 加」语境）----------------
    if any(k in text for k in ("换", "转粮", "加")):
        if "昨天" in text:
            result["change_days"] = 1
        elif "前天" in text:
            result["change_days"] = 2
        elif "今天" in text:
            result["change_days"] = 0
        else:
            m = re.search(rf"({_CN_NUM})\s*天前", text)
            if not m:
                m = re.search(rf"(?:换|转粮|加)[^0-9一二两三四五六七八九十]{{0,6}}?({_CN_NUM})\s*天", text)
            if m:
                n = _cn_to_int(m.group(1))
                if n is not None:
                    result["change_days"] = n

    # ---------------- 症状与风险组 ----------------
    # 先用「否定 + 关键词」判定 False，再判 True，避免「没便血」被当成「便血」
    _NEG = r"(?:没有|没|不|无|未)"

    if re.search(_NEG + r"(?:有|见到|看到)?(?:吐|呕吐)", text):
        result["vomiting"] = False
    elif re.search(r"(?:吐了|呕吐|有吐|吐过|呕了)", text):
        result["vomiting"] = True

    if re.search(_NEG + r"(?:有|见到|看到|拉)?(?:便血|血便|带血|拉血|见血|血丝|血点|血)", text):
        result["blood_in_stool"] = False
    elif re.search(r"(?:便血|血便|带血|拉血|有血|血丝|血点)", text):
        result["blood_in_stool"] = True

    if re.search(r"(?:食欲|胃口)(?:正常|还行|不错|没变|没减|挺好|很好)|"
                 r"(?:吃饭|吃|进食)正常|正常吃|吃得正常", text):
        result["appetite"] = "normal"
    elif re.search(r"(?:不吃|不肯吃|不爱吃|食欲差|食欲下降|食欲不好|食欲变差|"
                   r"不太吃|吃很少|没胃口|吃得少)", text):
        result["appetite"] = "poor"

    if re.search(r"精神(?:正常|挺好|不错|好|可以)|活泼|精神头好", text):
        result["energy"] = "normal"
    elif re.search(r"(?:没精神|精神差|精神不好|精神萎靡|蔫|萎靡|不爱动|嗜睡|没劲)", text):
        result["energy"] = "poor"

    if re.search(r"(?:吃饭|吃喝|食欲|胃口)(?:和|、)?精神(?:都|也)?(?:正常|挺好|很好|还行)", text):
        result["appetite"] = "normal"
        result["energy"] = "normal"

    return {k: v for k, v in result.items() if v is not None and k in SLOT_KEYS}


def _clean_product(raw: str):
    """去掉产品名尾部的语气词和标点。"""
    if not raw:
        return None
    raw = raw.strip().strip("的了")
    for tail in ("了", "吧", "呀", "啊", "呢", "啦"):
        if raw.endswith(tail) and len(raw) > 1:
            raw = raw[:-1]
    raw = raw.strip("的了")
    return raw or None


# =========================================================
# 2. 自然语言改写
# =========================================================
def generate_response_from_plan(state: dict, current_summary: str, question: str) -> str:
    """
    真实大模型上线时，把 current_summary + question_plan 交给模型润色。
    当前版本直接拼接即可（Python 已经决定好"说什么"）。
    """
    if not MOCK_LLM:
        return call_real_llm(_REWRITE_SYSTEM_PROMPT, json.dumps({
            "summary": current_summary,
            "question_or_assessment": question,
            "pattern": (state or {}).get("pattern"),
            "slots": (state or {}).get("slots", {}),
        }, ensure_ascii=False))

    summary = (current_summary or "").strip()
    question = (question or "").strip()
    if not question:
        return summary
    if not summary:
        return question
    return f"{summary}\n\n{question}"


# =========================================================
# 真实模型 prompt 模板
# =========================================================
_EXTRACT_SYSTEM_PROMPT = """
你是宠物健康对话的信息抽取模块。
只抽取用户明确表达的信息，不做医学判断，不补充猜测。
输出 JSON，未提及的字段一律返回 null。

字段：
recent_diet_change: 最近是否有饮食变化 true/false/null
change_type: main_food_change（换主粮）/ wet_food_added（加罐头）/ snack_added（加零食）/ null
old_product: 原来吃的产品名 字符串/null
new_product: 现在换成/新加的产品名 字符串/null
change_ratio: 新食物占日常饮食比例 0~1/null
change_days: 饮食变化从几天前开始 整数/null
soft_stool_days: 软便持续天数 整数/null
vomiting: 是否呕吐 true/false/null
blood_in_stool: 是否便血或明显黏液 true/false/null
appetite: normal/poor/null
energy: normal/poor/null
"""

_REWRITE_SYSTEM_PROMPT = """
你是宠物营养助手，把结构化的判断改写成自然、口语化、不吓人的中文回复。

硬约束：
- 不做确诊，只做「相关性判断」，必要时提醒线下就医；
- 不改动 summary 里给出的事实与数字；
- 若 question_or_assessment 是提问，就自然地问出来；若是结论，就地收尾，不要再反问；
- 不要输出 JSON，不要输出 markdown 标题，直接给对话文本。
"""
