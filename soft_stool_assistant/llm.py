"""Validated model extraction with offline fallbacks and two-layer responses."""

import json
import re

from .policy import BOOLEAN_FIELDS, SLOT_DEFAULTS, validate_extraction, urgent_risk

from openai import OpenAI

from app_config import get_chat_model_config
from .tools import DEMO_PRODUCT_ALIAS

MOCK_LLM = False

# 槽位组（与 pipeline.new_state 的 slots 一一对应）
SLOT_KEYS = tuple(SLOT_DEFAULTS)

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
        model=cfg["model"], temperature=0.2, response_format={"type": "json_object"},
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
def _extract_rule_info(user_input: str, state: dict) -> dict:
    """Offline extraction and explicit risk/negation checks."""
    text = (user_input or "").strip()
    raw_text = text
    result = {}
    unknown_keys = []
    uncertain = r"不知道|不清楚|不记得|记不清|不确定|不了解|如果|假如|担心|会不会"
    field_words = {
        "vomiting": r"呕吐|吐", "blood_in_stool": r"便血|血便|血", "mucus_in_stool": r"黏液|粘液",
        "appetite": r"食欲|胃口|吃饭", "energy": r"精神", "change_ratio": r"比例|占比",
        "soft_stool_days": r"软便.*(?:多久|几天|时间)", "change_days": r"(?:变化|换粮|开始).*(?:多久|几天|时间)",
    }
    certain_clauses = []
    for clause in re.split(r"[，。,；;]|但是|不过|但", text):
        if re.search(uncertain, clause):
            unknown_keys.extend(k for k, words in field_words.items() if re.search(words, clause))
        else:
            certain_clauses.append(clause)
    text = "，".join(certain_clauses)
    if unknown_keys:
        result["unknown_fields"] = unknown_keys

    clauses = re.split(r"[，。,；;]|但是|不过|但", text)
    future_change = r"(?:打算|计划|准备|考虑|想)(?:要)?(?:换|加|添加).*"
    diet_clauses = [re.sub(future_change, "", c) for c in clauses]
    diet_text = "，".join(diet_clauses)
    events = {
        "main_food_changed": (r"换(?:主粮|粮|新粮|了粮|了个粮|了)|换成|换到|改喂|改吃|转粮", "main_food_change"),
        "wet_food_added": (r"(?:加|添加|开始吃|开始喂|新吃).*(?:罐头|湿粮)|(?:罐头|湿粮).*(?:新加|加了)", "wet_food_added"),
        "snack_added": (r"(?:加|添加|开始吃|开始喂|新吃).*零食|零食.*(?:新加|加了)", "snack_added"),
        "other_food_added": (r"(?:加|添加|开始吃).*(?:新食物|新东西|新的东西)", "other_food_added"),
    }
    for field, (pattern, _) in events.items():
        for clause in diet_clauses:
            match = re.search(pattern, clause)
            if match:
                negative = bool(re.search(r"(?:没有|没|未|不)(?:有|再)?$", clause[:match.start()]))
                result[field] = not negative
    if re.search(r"(?:没|没有)(?:任何)?(?:饮食)?变化|一直吃|还是原来", text):
        result["recent_diet_change"] = False
    positive = [kind for field, (_, kind) in events.items() if result.get(field) is True]
    if positive:
        result["recent_diet_change"] = True
        result["change_type"] = positive[0] if len(positive) == 1 else "multiple"
    elif any(field in result for field in events):
        # Denying a main-food switch does not deny additions or all diet changes.
        if all(result.get(field) is False for field in events):
            result["recent_diet_change"] = False
    added = [clause.strip() for clause in diet_clauses if re.search(r"罐头|湿粮|零食|新食物", clause)
             and not re.search(r"(?:没有|没|未)(?:有)?(?:加|添加|吃)", clause)]
    if added and any(result.get(k) for k in ("wet_food_added", "snack_added", "other_food_added")):
        result["added_food"] = "、".join(added)

    # Preserve current food separately; it is not automatically the pre-switch food.
    old = re.search(r"(?:原来|原本|以前|之前)(?:一直)?(?:吃|喂|是)?\s*(.+?)(?=[，。,；;、]|现在|换成|换到|改喂|改吃|$)", diet_text)
    current = re.search(r"(?:(?:目前|现在|一直|正在|平时|主要)(?:吃|喂)|吃的是|喂的是)(?:的是|的|是)?\s*([^，。,；;、]+)", diet_text)
    new = re.search(r"(?:换成|换到|改吃|改喂|新粮是|新粮换成)\s*([^，。,；;、]+)", diet_text)
    if old:
        result["old_product"] = _clean_product(old.group(1))
    if current:
        result["current_product"] = _clean_product(current.group(1))
    if new:
        result["new_product"] = _clean_product(new.group(1))
    elif current and (result.get("main_food_changed") is True or (state or {}).get("slots", {}).get("main_food_changed") is True):
        result["new_product"] = result["current_product"]

    # 昵称兜底（A粮 / B粮 / C粮 …）
    for name in _ALIAS_NAMES:
        if name in diet_text:
            if any(k in diet_text for k in ("原来", "以前", "之前", "原本")):
                result.setdefault("old_product", name)
            else:
                result.setdefault("new_product", name)

    # ---------------- 比例 ----------------
    m = re.search(rf"({_CN_NUM})\s*成", diet_text)
    if m:
        n = _cn_to_int(m.group(1))
        if n is not None and 0 <= n <= 10:
            result["change_ratio"] = n / 10.0
    if "一半" in diet_text:
        result["change_ratio"] = 0.5
    m = re.search(r"(\d{1,3})\s*%", diet_text)
    if m:
        pct = int(m.group(1))
        if 0 <= pct <= 100:
            result["change_ratio"] = pct / 100.0

    # ---------------- 软便持续天数（先抽，避免被换粮天数误用）----------------
    symptom_text = "，".join(c for c in clauses if re.search(r"软便|拉稀|稀便|腹泻", c))
    for pattern in (
        rf"软便\D{{0,4}}?({_CN_NUM})\s*天",
        rf"(?:持续|已经|有)?({_CN_NUM})\s*天(?:前|了)?(?:开始)?软便",
        rf"拉稀\D{{0,4}}?({_CN_NUM})\s*天",
    ):
        m = re.search(pattern, symptom_text)
        if m:
            n = _cn_to_int(m.group(1))
            if n is not None:
                result["soft_stool_days"] = n
            break
    if "soft_stool_days" not in result:
        if symptom_text:
            if "昨天" in symptom_text:
                result["soft_stool_days"] = 1
            elif "前天" in symptom_text:
                result["soft_stool_days"] = 2

    # ---------------- 换粮天数（必须有「换 / 加」语境）----------------
    group = (state or {}).get("context", {}).get("active_question_group")
    change_text = "，".join(c for c in diet_clauses if re.search(r"换|转粮|加|变化|开始", c) and not re.search(r"软便|拉稀|呕吐|便血", c))
    if group == "diet_change_detail_group" and not change_text:
        change_text = "，".join(c for c in diet_clauses if not re.search(r"软便|拉稀|呕吐|便血", c))
    if result.get("recent_diet_change") is True or group == "diet_change_detail_group":
        if "昨天" in change_text:
            result["change_days"] = 1
        elif "前天" in change_text:
            result["change_days"] = 2
        elif "今天" in change_text:
            result["change_days"] = 0
        else:
            m = re.search(rf"({_CN_NUM})\s*天前", change_text)
            if not m:
                m = re.search(rf"(?:换|转粮|加)[^0-9一二两三四五六七八九十]{{0,6}}?({_CN_NUM})\s*天", change_text)
            if m:
                n = _cn_to_int(m.group(1))
                if n is not None:
                    result["change_days"] = n

    # ---------------- 症状与风险组 ----------------
    for clause in clauses:
        if re.search(r"以前|曾经|去年|病史|病历|之前|原来", clause) and not re.search(r"今天|现在|最近", clause):
            continue
        for field, negative, positive_pattern in (
            ("vomiting", r"(?:没有|没|不|无|未)(?:有)?(?:呕吐|吐)", r"呕吐|吐了|吐过|有吐"),
            ("blood_in_stool", r"(?:没有|没|无|未)(?:有|见到|看到|拉)?(?:便血|血便|带血|血)", r"便血|血便|带血|拉血|见血|血丝|血点|有血(?!常规|压|糖|检)"),
            ("mucus_in_stool", r"(?:没有|没|无|未)(?:有|明显)?(?:黏液|粘液)", r"黏液|粘液"),
        ):
            if re.search(negative, clause):
                result[field] = False
            elif re.search(positive_pattern, clause):
                result[field] = True

    for clause in clauses:
        if re.search(r"以前|曾经|去年|病史|病历|之前|原来", clause) and not re.search(r"今天|现在|最近", clause):
            continue
        if re.search(r"(?:食欲|胃口)(?:已经|也|都)?(?:正常|还行|不错|没变|没减|挺好|很好|没有下降|没下降)|(?:吃饭|进食)正常|正常吃|吃得正常", clause):
            result["appetite"] = "normal"
        elif re.search(r"不吃|不肯吃|不爱吃|食欲差|食欲下降|食欲不好|食欲变差|不太吃|吃很少|没胃口|吃得少", clause):
            result["appetite"] = "poor"
        if re.search(r"精神(?:已经|也|都)?(?:正常|挺好|不错|好|可以|没变差|没有变差)|活泼|精神头好", clause):
            result["energy"] = "normal"
        elif re.search(r"没精神|精神差|精神不好|精神变差|精神萎靡|蔫|萎靡|不爱动|嗜睡|没劲", clause):
            result["energy"] = "poor"
        if re.search(r"(?:吃饭|吃喝|食欲|胃口)(?:和|、)?精神(?:都|也)?(?:正常|挺好|很好|还行)", clause):
            result.update(appetite="normal", energy="normal")

    context = (state or {}).get("context") or {}
    group = context.get("active_question_group")
    asked = context.get("active_question_fields") or []
    short = raw_text.strip("。！! ")
    if re.fullmatch(r"不知道|不清楚|不记得|记不清|不确定|不了解|不想说|不想回答|别问了|跳过|先跳过|不回答", short):
        result["unknown_fields"] = list(asked)
    if re.fullmatch(r"没有|没|无|都没有|没有这些|没有了", short):
        for key in asked:
            if key in BOOLEAN_FIELDS:
                result[key] = False
        if group == "recent_diet_change_group":
            result["recent_diet_change"] = False
        if group == "risk_group":
            result.update(appetite="normal", energy="normal")
    if group == "risk_group" and re.fullmatch(r"正常|都正常|都挺好|都还好", short):
        result.update(appetite="normal", energy="normal")
    if group in ("symptom_group", "diet_change_detail_group"):
        duration = re.fullmatch(rf"(?:大约|大概)?({_CN_NUM})天(?:前|了)?", short)
        key = "soft_stool_days" if group == "symptom_group" else "change_days"
        if duration and key in asked:
            result[key] = _cn_to_int(duration.group(1))
    name_fields = [key for key in asked if key in ("current_product", "old_product", "new_product", "added_food")]
    if len(name_fields) == 1 and re.fullmatch(r"[\w\u4e00-\u9fff ·_-]{2,80}", short) and not re.search(
        r"不知道|不清楚|没有|正常|软便|呕吐|便血|好的|知道了|^嗯+$", short
    ) and not result:
        result[name_fields[0]] = short
    if group == "history_and_disease_group":
        if re.fullmatch(r"没有|都没有|没有其他(?:的)?|没有其他疾病和主粮", short):
            result.update(history_foods_reviewed="没有其他主粮", disease_history_reviewed="没有其他疾病")
        else:
            if re.search(r"(?:以前|之前|历史|原来).*(?:吃|喂)|没有其他主粮", text):
                result["history_foods_reviewed"] = text
            if re.search(r"(?:疾病|病史|病历|得过|诊断|肠胃炎|过敏)", text):
                result["disease_history_reviewed"] = text
    if re.search(future_change, raw_text) and not positive:
        result.pop("change_ratio", None)
        result.pop("change_days", None)
    return validate_extraction(result)


def extract_user_info(user_input: str, state: dict) -> dict:
    rule = _extract_rule_info(user_input, state)
    # Explicit danger must not wait on a remote model or product lookups.
    if urgent_risk(rule) or MOCK_LLM or not get_chat_model_config().get("api_key"):
        return rule
    try:
        parsed = _parse_json_object(call_real_llm(_EXTRACT_SYSTEM_PROMPT, json.dumps({
            "user_input": user_input,
            "current_slots": (state or {}).get("slots", {}),
            "last_question": (state or {}).get("context", {}).get("active_question_fields", []),
            "output_fields": list(SLOT_DEFAULTS),
        }, ensure_ascii=False)))
        extracted = validate_extraction(parsed)
        if re.search(r"(?:打算|计划|准备|考虑|想)(?:要)?(?:换|加|添加)", user_input):
            # Do not turn proposed changes or a planned percentage into feeding facts.
            for key in ("main_food_changed", "wet_food_added", "snack_added", "other_food_added",
                        "new_product", "added_food", "change_ratio", "change_days", "recent_diet_change", "change_type"):
                if key not in rule:
                    extracted.pop(key, None)
        # Explicit unknowns must not be replaced by model guesses.
        for key in rule.get("unknown_fields", []):
            extracted.pop(key, None)
        # Literal risk and negation take priority over model interpretation.
        extracted.update(rule)
        return extracted
    except Exception:
        return rule


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
def _public_mechanism_analysis(analysis: dict) -> dict:
    """Keep provenance for model reasoning; it need not be displayed to the user."""
    return {k: v for k, v in analysis.items() if k in {
        "reference_pool_version", "reference_pool_size", "confidence",
        "mechanisms", "amplifiers", "reasoning_rule",
    }}


def _product_evidence(state):
    baseline = state.get("context", {}).get("baseline_diet") or {}
    products = list(baseline.get("detailed_products") or [])
    new = state.get("analysis", {}).get("new_product_detail")
    if new:
        products.append(new)
    result, seen = [], set()
    for product in products[:6]:
        identity = product.get("catalog_key") or product.get("name")
        if identity in seen:
            continue
        seen.add(identity)
        result.append({
            "name": product.get("display_name") or product.get("name"),
            "guarantee_values": product.get("nutrition_metrics") or {},
            "protein_sources": product.get("protein_sources") or [],
            "protein_source_detail": product.get("protein_source_detail"),
            "fat_sources": product.get("fat_sources") or [],
            "gut_friendly_score": product.get("gut_friendly_score"),
            "soft_stool_risk": product.get("soft_stool_risk") or {},
            "soft_stool_mechanism_analysis": _public_mechanism_analysis(product.get("soft_stool_mechanisms") or {}),
        })
    return result


def _mechanism_fallback(state):
    # A hypothesis stays scoped to its product; never combine separate foods' scores.
    parts = []
    for product in _product_evidence(state):
        analysis = product["soft_stool_mechanism_analysis"]
        if not analysis:
            continue
        primary = [m["mechanism"] for m in analysis.get("mechanisms", []) if m.get("role") == "primary"]
        secondary = [m["mechanism"] for m in analysis.get("mechanisms", []) if m.get("role") == "secondary" and m.get("contribution", 0) >= .08]
        amplifiers = [m["mechanism"] for m in analysis.get("amplifiers", [])]
        detail = []
        if primary:
            detail.append("、".join(primary) + ("是共同关注的配方因素" if len(primary) > 1 else "是主要关注的配方因素"))
        if secondary:
            detail.append(secondary[0] + "为次要观察因素")
        if amplifiers:
            detail.append("、".join(amplifiers) + "可能放大表现")
        if analysis.get("confidence") == "limited":
            detail.append("可用于比较的数据有限，解释需要保留不确定性")
        if detail:
            parts.append(product["name"] + "：" + "；".join(detail) + "。")
    return " ".join(parts) + ("这些是配方层面的参考，不能单独证明这次软便的原因。" if parts else "暂缺足够产品证据，先结合饮食变化和症状表现判断。")


def _valid_text(text, factual_context, action=False):
    if not isinstance(text, str) or not text.strip() or len(text) > (300 if action else 600):
        return False
    if re.search(r"确诊为|一定是|肯定是|保证改善|治疗剂量|服用|喂药|禁食|停药|每公斤|毫克|不用就医|无需就医|不需要就医|\bmg\b", text):
        return False
    # No invented numerical treatment/feeding schedule; exact parameters need policy.
    if action and re.search(r"[0-9]|[一二两三四五六七八九十]+(?:天|小时|克|毫升|成)|百分之|一半|换成|购买|买一|推荐.*(?:品牌|产品|猫粮)", text):
        return False
    numbers = set(re.findall(r"\d+(?:\.\d+)?", text))
    return numbers <= set(re.findall(r"\d+(?:\.\d+)?", factual_context))


def generate_two_stage_response(state, evidence_summary, current_summary, question):
    """Facts remain server-owned; the model explains evidence and proposes actions."""
    plan = state.get("analysis", {}).get("assessment_plan") or {}
    mechanism = _mechanism_fallback(state)
    reasoning = mechanism + " " + plan.get("judgment", "目前只能进行初步关联分析。")
    actions = list(plan.get("next_steps") or [])
    observe = " ".join(plan.get("observe") or [])
    review = plan.get("review", "")
    payload = {
        "facts": current_summary, "available_product_evidence": _product_evidence(state),
        "slots": state.get("slots", {}), "slot_status": state.get("slot_status", {}),
        "pet_profile": state.get("context", {}).get("pet_profile", {}),
        "assessment_plan": plan, "next_question": question,
        "output_schema": {"reasoning_message": "机制与个体情况的初步关联解释",
                          "action_steps": ["可执行的下一步"], "observation": "观察项", "review": "复评条件"},
    }
    state["analysis"]["generation_source"] = "fallback"
    state["analysis"]["model_name"] = None
    try:
        cfg = get_chat_model_config()
        if cfg.get("api_key"):
            parsed = _parse_json_object(call_real_llm(_TWO_STAGE_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False)))
            candidate = parsed.get("reasoning_message")
            if _valid_text(candidate, json.dumps(payload, ensure_ascii=False)):
                # Preserve the rule's mechanism ordering and any timing counter-evidence.
                reasoning = mechanism + " " + plan.get("judgment", "") + " " + candidate.strip()
                state["analysis"].update(generation_source="model", model_name=cfg.get("model"))
            proposed = parsed.get("action_steps")
            if not question and isinstance(proposed, list) and 1 <= len(proposed) <= 3:
                if all(_valid_text(x, current_summary, action=True) for x in proposed):
                    actions = [x.strip() for x in proposed]
                    state["analysis"].update(generation_source="model", model_name=cfg.get("model"))
                if _valid_text(parsed.get("observation"), current_summary, action=True):
                    observe = parsed["observation"].strip()
                # Keep the server's escalation condition even if the model omits it.
                if _valid_text(parsed.get("review"), current_summary, action=True):
                    review = parsed["review"].strip() + " " + review
    except Exception:
        pass
    if question:
        reply = reasoning.strip() + "\n\n" + question
        response_type = "reasoning_followup"
    else:
        reply = reasoning.strip() + "\n\n下一步：" + "；".join(actions) + "\n观察：" + observe + "\n复评：" + review
        response_type = "result_card"
    return [{"response_type": "evidence_card", "reply": evidence_summary},
            {"response_type": response_type, "reply": reply}]


def generate_response_from_plan(state, current_summary, question):
    """Compatibility helper for callers expecting a single reply."""
    return current_summary + "\n\n" + (question or "")


_EXTRACT_SYSTEM_PROMPT = """
你是宠物咨询信息抽取模块，只返回JSON，不诊断，不补猜信息。
只抽取本轮用户明确表达的新事实或纠正，不复制current_slots中的旧事实。
结合last_question理解“没有”“两天”等省略回答；无法判断就不填写。
输出字段由output_fields限定。未提及的字段省略。unknown_fields列出用户明确不知道或拒答的字段。
所有布尔字段必须是true/false；change_days与soft_stool_days是非负整数；change_ratio是0到1的数字。
appetite、energy取normal或poor。history_foods_reviewed与disease_history_reviewed分别记录相关回答。
main_food_changed、wet_food_added、snack_added、other_food_added分别判断，不把“没换粮”当作没有添加其他食物。
“打算、准备、想换粮”是计划，不是已经发生的饮食变化，不填写实际换粮时间和比例。
change_type取main_food_change/wet_food_added/snack_added/other_food_added/multiple。
current_product是当前确认在吃的主粮，不自动当成换粮前的粮；old_product是换粮前主粮，new_product是换成的主粮，added_food是新加罐头、零食或其他食物。
blood_in_stool表示血，mucus_in_stool表示黏液，必须分开。任何unknown都不能填成false或normal。
疾病和历史饮食只有用户明确作答才记录，不能把任意一句话当成同时回答两个问题。
"""

_TWO_STAGE_SYSTEM_PROMPT = """
你是宠物营养咨询助手，根据输入事实、已有机制、时间关系和用户补充信息，解释可能关联并提出下一步。
只返回reasoning_message、action_steps、observation、review四个字段，后三项用于没有next_question时的最终建议。
事实卡和next_question由后端原样输出，你不要重写问题。信息有限时也给可执行的记录、观察或补充动作。
规则已提供共同主导、次要因素与支持不足，不重新排序或新增未提供的机制。
不要把配方指标、品牌反馈或模型指数解释为个体患病概率，不确诊，不承诺疗效。
保留“不确定”和时间反证；若软便先于饮食变化，不能说该变化引起了软便。
参考池和可信度用于内部判断；confidence=limited时使用“资料有限”等克制表达，不展示内部版本和样本数。
不要推荐库外具体产品、药物、剂量、禁食、停药或没有规则依据的精确天数、比例和喂食量。
使用已有事实，不能猜测新产品数据、基础疾病或营养值。具体病历信息只作背景，不自行推导诊断。
行动建议需要说明用户接下来做什么、记录什么、何时复评，避免只有泛泛的指标解释。
"""
