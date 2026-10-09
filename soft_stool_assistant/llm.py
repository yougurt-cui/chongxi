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

from openai import OpenAI

from app_config import get_chat_model_config
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
    """Call the configured chat model through its OpenAI-compatible API."""
    cfg = get_chat_model_config()
    if not cfg.get("api_key"):
        raise RuntimeError("聊天模型未配置")
    options = {}
    if cfg.get("provider") == "deepseek":
        options["extra_body"] = {"thinking": {"type": "disabled"}}
    response = OpenAI(
        api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=45,
    ).chat.completions.create(
        model=cfg["model"], temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        **options,
    )
    return (response.choices[0].message.content or "").strip()


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


def _parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        value = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    return value if isinstance(value, dict) else {}


# =========================================================
# 2. 自然语言改写
# =========================================================
def generate_response_from_plan(state: dict, current_summary: str, question: str) -> str:
    """
    真实大模型上线时，把 current_summary + question_plan 交给模型润色。
    当前版本直接拼接即可（Python 已经决定好"说什么"）。
    """
    baseline = ((state or {}).get("context") or {}).get("baseline_diet") or {}
    product_evidence = []
    for product in (baseline.get("detailed_products") or [])[:5]:
        product_evidence.append({
            "name": product.get("display_name") or product.get("name"),
            "guarantee_values": product.get("nutrition_metrics") or {},
            "protein_sources": product.get("protein_sources") or [],
            "protein_source_detail": product.get("protein_source_detail"),
            "fat_sources": product.get("fat_sources") or [],
            "prebiotics": product.get("prebiotics") or [],
            "gut_friendly_score": product.get("gut_friendly_score"),
            "function_scores": product.get("function_scores") or {},
            "soft_stool_risk": product.get("soft_stool_risk") or {},
            "soft_stool_mechanism_analysis": product.get("soft_stool_mechanisms") or {},
        })

    payload = {
            "summary": current_summary,
            "question_or_assessment": question,
            "pattern": (state or {}).get("pattern"),
            "slots": (state or {}).get("slots", {}),
            "available_product_evidence": product_evidence,
            "pet_history_context": {
                "history_food_names": ((state or {}).get("context") or {}).get("history_food_names", []),
                "disease_names": ((state or {}).get("context") or {}).get("disease_names", []),
            },
        }

    # Slot extraction remains deterministic, while response wording uses the
    # configured model.  Fall back to the verified plan if the model is absent
    # or temporarily unavailable.
    try:
        if get_chat_model_config().get("api_key"):
            rewritten = call_real_llm(
                _REWRITE_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False),
            )
            if rewritten:
                return rewritten
    except Exception:
        pass

    summary = (current_summary or "").strip()
    question = (question or "").strip()
    if not question:
        return summary
    if not summary:
        return question
    return f"{summary}\n\n{question}"


def _mechanism_fallback(state: dict) -> str:
    baseline = ((state or {}).get("context") or {}).get("baseline_diet") or {}
    product = next((
        item for item in (baseline.get("detailed_products") or [])
        if item.get("soft_stool_mechanisms")
    ), None)
    if not product:
        return "目前只能先结合饮食变化和症状表现继续判断。"
    analysis = product["soft_stool_mechanisms"]
    primary = [
        item["mechanism"] for item in analysis.get("mechanisms", [])
        if item.get("role") == "primary"
    ]
    secondary = [
        item["mechanism"] for item in analysis.get("mechanisms", [])
        if item.get("role") == "secondary" and item.get("contribution", 0) >= 0.08
    ]
    amplifiers = [item["mechanism"] for item in analysis.get("amplifiers", [])]
    parts = []
    if primary:
        parts.append("从相对值看，目前更需要关注" + "和".join(primary) + "，两者可能共同影响消化适应")
    if secondary:
        parts.append(secondary[0] + "是次要观察因素")
    if amplifiers:
        parts.append("另外" + "、".join(amplifiers) + "可能放大便便变软的表现")
    if analysis.get("confidence") == "limited":
        parts.append(f"不过历史参考池目前只有{analysis.get('reference_pool_size')}个产品，这个结论需要保留不确定性")
    return "；".join(parts) + ("。" if parts else "")


def generate_two_stage_response(
    state: dict,
    evidence_summary: str,
    current_summary: str,
    question: str,
) -> list[dict]:
    """Return an evidence bubble followed by reasoning plus the next question."""
    baseline = ((state or {}).get("context") or {}).get("baseline_diet") or {}
    product_evidence = []
    for product in (baseline.get("detailed_products") or [])[:5]:
        product_evidence.append({
            "name": product.get("display_name") or product.get("name"),
            "guarantee_values": product.get("nutrition_metrics") or {},
            "protein_sources": product.get("protein_sources") or [],
            "protein_source_detail": product.get("protein_source_detail"),
            "fat_sources": product.get("fat_sources") or [],
            "gut_friendly_score": product.get("gut_friendly_score"),
            "function_scores": product.get("function_scores") or {},
            "soft_stool_risk": product.get("soft_stool_risk") or {},
            "soft_stool_mechanism_analysis": product.get("soft_stool_mechanisms") or {},
        })
    payload = {
        "evidence_summary": evidence_summary,
        "current_context_summary": current_summary,
        "available_product_evidence": product_evidence,
        "slots": (state or {}).get("slots", {}),
        "next_question": question,
        "pet_history_context": {
            "history_food_names": ((state or {}).get("context") or {}).get("history_food_names", []),
            "disease_names": ((state or {}).get("context") or {}).get("disease_names", []),
        },
        "output_schema": {
            "evidence_message": "只陈述证据层事实的字符串",
            "reasoning_message": "初步机制结论与证据边界的字符串",
            "followup_question": "必须与next_question含义一致的字符串",
        },
    }
    try:
        if get_chat_model_config().get("api_key"):
            raw = call_real_llm(_TWO_STAGE_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False))
            parsed = _parse_json_object(raw)
            evidence = str(parsed.get("evidence_message") or "").strip()
            reasoning = str(parsed.get("reasoning_message") or "").strip()
            followup = str(parsed.get("followup_question") or "").strip()
            if evidence and reasoning and followup:
                return [
                    {"response_type": "evidence_card", "reply": evidence},
                    {"response_type": "reasoning_followup", "reply": f"{reasoning}\n\n{followup}"},
                ]
    except Exception:
        pass
    reasoning = _mechanism_fallback(state)
    return [
        {"response_type": "evidence_card", "reply": evidence_summary},
        {"response_type": "reasoning_followup", "reply": f"{reasoning}\n\n{question}".strip()},
    ]


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
- available_product_evidence 是可用的产品证据。即使保证值不完整，也要自然利用蛋白来源、脂肪来源、功能评分和软便风险等已有信息；
- soft_stool_mechanism_analysis 已由规则层按历史参考池分位和模型权重计算。按其中 role、contribution、percentile、amplifiers 做总结和逻辑推理，不得自行改变主次顺序；
- role=primary 且有两项时，应表述为“共同主导”，不要强行只选第一项；amplifiers 是可能放大表现的保护不足，不是独立病因；
- confidence=limited 或 reference_pool_size<30 时，应自然提示“参考样本有限”，避免使用确定语气；
- “风险等级/风险指数/肠胃友好评分”只能解释为数据库模型的辅助观察信号，不得说成该产品导致软便，也不得把它当成医学诊断；
- 不要把“保证值不完整”写成“产品没有数据”，只需简短说明无法精确计算蛋白、脂肪和粗纤维变化；
- 面向普通养宠用户解释，避免 formula_id、product_key、槽位、模型字段名等技术术语；
- 若 question_or_assessment 是提问，就自然地问出来；若是结论，就地收尾，不要再反问；
- question_or_assessment 若询问历史主粮或其他疾病，必须原样保留该问句，不追加其他问题；
- 历史饮食只问主粮，不询问罐头、零食、营养品、比例或混喂情况；
- 追问不超过30个汉字，不使用按钮、选项、列表或表单话术；
- 不要输出 JSON，不要输出 markdown 标题，直接给对话文本。
"""

_TWO_STAGE_SYSTEM_PROMPT = """
你是宠物营养助手。规则层已经完成数据查询、历史分位计算、机制贡献排序和下一轮问题规划。
你只负责把结果整理成两条自然、准确、普通养宠用户能理解的中文消息，并只输出合法JSON。

第一条 evidence_message：
- 只展示数据库事实、相对分位、评分和风险辅助信号，不下因果结论；
- 清楚区分“保证值缺失”和“没有其他数据”；
- 优先展示与软便有关的证据，其他功能评分可简洁归纳；
- 说明风险模型和品牌反馈只是辅助证据，不能证明产品导致软便。

第二条 reasoning_message：
- 严格按照 soft_stool_mechanism_analysis 的 role、contribution、percentile 和 amplifiers 推理；
- 两项 role=primary 时必须表述为共同主导，不得强行只选一个；
- amplifiers 是保护不足或放大因素，不是独立病因；
- 不得擅自改变机制排序，不得补充输入中不存在的事实；
- 没有粗蛋白保证值时不得说“高蛋白”；没有实测消化率时不得说“消化率下降”；
- confidence=limited 或参考池少于30个产品时，必须说明样本有限并保留不确定性；
- 只能表述配方相关性，不得确诊，不得说“一定是这款粮导致”。

followup_question：
- 必须保留 next_question 的业务含义；
- 不得遗漏或改成其他问题；
- 需要追加收集信息时，先参考 pet_history_context 中查到的记录，再自然询问用户补充；
- 历史饮食只问主粮，不询问罐头、零食、营养品、比例或混喂情况；
- 有历史主粮时使用“记录显示吃过XXX，最近还吃过哪些主粮？”；没有时使用“没查到饮食记录，最近吃过哪些主粮？”；
- 有疾病档案时使用“记录显示有XXX，还得过其他疾病吗？”；没有时使用“没查到疾病记录，以前得过其他疾病吗？”；
- 每个追问不超过30个汉字，不使用按钮、选项、列表或表单话术；
- 如果输入已经提示便血、频繁呕吐、精神或食欲明显变差，应优先建议就医，不继续普通配方归因。

输出必须正好包含 evidence_message、reasoning_message、followup_question 三个字符串字段，不要输出Markdown代码块。
"""
