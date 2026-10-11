"""Soft-stool consultation: evidence, grouped follow-ups and an actionable plan."""

from copy import deepcopy

import pymysql

from . import policy
from .tools import (
    build_baseline_diet_profile, get_product_by_name,
    build_manual_old_new_compare, get_recent_diet_change, get_pet_profile,
)
from .llm import extract_user_info, generate_two_stage_response

PATTERN_WITH_BASELINE = "with_baseline_diet"
PATTERN_WITHOUT_BASELINE = "without_baseline_diet"
DATA_ERRORS = (pymysql.MySQLError, LookupError, ValueError)


def empty_baseline():
    return {"has_diet_data": False, "products": [], "detailed_products": [],
            "nutrition_summary": None, "notes": []}


def new_state(pet_id):
    return {
        "pet_id": pet_id, "pipeline": "soft_stool", "turn": 0,
        "status": "active", "rule_version": policy.RULE_VERSION,
        "pattern": PATTERN_WITHOUT_BASELINE,
        "context": {"baseline_loaded": False, "baseline_diet": empty_baseline(),
                    "diet_override": None, "prefill": True,
                    "followup_context_loaded": False, "history_food_names": [],
                    "disease_names": [], "active_question_group": None,
                    "active_question_fields": []},
        "slots": dict(policy.SLOT_DEFAULTS), "slot_status": {},
        "question_attempts": {},
        "analysis": {"diet_compare": None, "data_warnings": [], "current_summary": None},
    }


def update_slots(state, extracted):
    values = policy.validate_extraction(extracted)
    statuses = state.setdefault("slot_status", {})
    changed = []
    evidence = extracted.get("_field_evidence") or {}
    for key, value in values.items():
        if key == "unknown_fields":
            continue
        if key == "current_product" and state["slots"].get(key) != value:
            state["context"]["baseline_loaded"] = False
        if state["slots"].get(key) != value or statuses.get(key, {}).get("status") != "confirmed":
            changed.append(key)
        state["slots"][key] = value
        statuses[key] = {"status": "confirmed", "source": "user_message", "turn": state["turn"],
                         "source_text": evidence.get(key)}
    for key in values.get("unknown_fields", []):
        # A user's explicit correction to unknown invalidates an older value.
        if key == "current_product":
            state["context"]["baseline_loaded"] = False
        state["slots"][key] = None
        statuses[key] = {"status": "unknown", "source": "user_message", "turn": state["turn"]}
        changed.append(key)
    if state["slots"].get("recent_diet_change") is False:
        for key in ("change_type", "old_product", "new_product", "added_food", "change_days", "change_ratio"):
            state["slots"][key] = None
            statuses.pop(key, None)
        for key in ("main_food_changed", "wet_food_added", "snack_added", "other_food_added"):
            state["slots"][key] = False
    flags = [(key, kind) for key, kind in (
        ("main_food_changed", "main_food_change"), ("wet_food_added", "wet_food_added"),
        ("snack_added", "snack_added"), ("other_food_added", "other_food_added"),
    ) if state["slots"].get(key) is True]
    if not flags and any(key in values for key in ("main_food_changed", "wet_food_added", "snack_added", "other_food_added")) and "recent_diet_change" not in values:
        state["slots"]["recent_diet_change"] = None
        statuses.pop("recent_diet_change", None)
    if flags and "recent_diet_change" not in values.get("unknown_fields", []):
        state["slots"]["recent_diet_change"] = True
        state["slots"]["change_type"] = flags[0][1] if len(flags) == 1 else "multiple"
    state["analysis"]["changed_fields"] = changed


def _warn(state, code):
    warnings = state["analysis"].setdefault("data_warnings", [])
    if code not in warnings:
        warnings.append(code)


def load_baseline_context(state):
    context = state["context"]
    if context["baseline_loaded"]:
        return
    try:
        override = context.get("diet_override")
        if state["slots"].get("current_product"):
            override = {"products": [{"ref": state["slots"]["current_product"], "ratio": 1.0}]}
        baseline = build_baseline_diet_profile(state["pet_id"], override=override)
    except DATA_ERRORS:
        baseline = empty_baseline()
        _warn(state, "product_data_unavailable")
    context["baseline_diet"] = baseline
    state["analysis"]["show_product_evidence"] = True
    context["baseline_loaded"] = True
    state["pattern"] = PATTERN_WITH_BASELINE if baseline.get("has_diet_data") else PATTERN_WITHOUT_BASELINE


def load_followup_context(state):
    context = state["context"]
    if context.get("followup_context_loaded"):
        return
    context["followup_context_loaded"] = True
    try:
        profile = get_pet_profile(state["pet_id"])
        context["pet_profile"] = {key: profile.get(key) for key in (
            "name", "animal_type", "age_months", "weight_kg", "allergies", "diseases",
        )}
        context["disease_names"] = [str(x) for x in profile.get("diseases") or []]
    except DATA_ERRORS:
        profile = {}
        _warn(state, "health_history_unavailable")
    try:
        from services.miniprogram_diet_history_service import list_diet_history, compact_diet_context
        if not profile.get("user_id"):
            raise LookupError("Pet owner unavailable")
        history = list_diet_history(profile["user_id"], state["pet_id"], limit=10)
        context["diet_history"] = compact_diet_context(history["items"])
        context["diet_association_note"] = history["association_note"]
        context["history_food_names"] = list(dict.fromkeys(
            " ".join(filter(None, (item["brand"], item["product"])))
            for item in history["items"] if not item["is_current"]
        ))
    except DATA_ERRORS:
        _warn(state, "diet_history_unavailable")
    if not context.get("prefill", True):
        return
    try:
        change = get_recent_diet_change(state["pet_id"])
        if change.get("found"):
            # An intent and its creation date are not confirmed feeding events.
            context["diet_intent_reference"] = {key: change.get(key) for key in (
                "old_product", "new_product", "source", "created_at",
            )}
    except DATA_ERRORS:
        _warn(state, "diet_history_unavailable")


def prefill_slots_from_history(state):
    """Compatibility entry point: intentions remain references, not user facts."""
    load_followup_context(state)


def _product_facts(product):
    name = product.get("display_name") or product.get("name") or "该产品"
    parts = [name]
    for key, label in (("protein_sources", "蛋白来源"), ("fat_sources", "脂肪来源")):
        if product.get(key):
            parts.append(label + "：" + "、".join(product[key]))
    if product.get("gut_friendly_score") is not None:
        parts.append(f"肠胃友好评分 {product['gut_friendly_score']}")
    risk = product.get("soft_stool_risk") or {}
    if risk.get("risk_level"):
        parts.append("辅助模型风险档位：" + risk["risk_level"])
    if risk.get("risk_index") is not None:
        parts.append(f"模型风险指数 {risk['risk_index']}（不是个体发病概率）")
    return "；".join(parts) + "。"


def summarize_baseline_diet(state):
    baseline = state["context"].get("baseline_diet") or empty_baseline()
    products = baseline.get("detailed_products") or baseline.get("products") or []
    if not products:
        return "目前没有可用于分析的完整产品资料，先结合你补充的饮食和症状信息判断。"
    parts = ["当前档案中的饮食资料：" + " ".join(_product_facts(p) for p in products[:5])]
    summary = baseline.get("nutrition_summary") or {}
    if summary:
        metrics = [f"{label}{(summary.get('metric_specs') or {}).get(key, {}).get('operator') or ''} {summary[key]}%" for key, label in (
            ("protein", "粗蛋白"), ("fat", "粗脂肪"), ("fiber", "粗纤维"),
        ) if summary.get(key) is not None]
        if metrics:
            parts.append("标示保证值参考：" + "、".join(metrics) + "（" + str(summary.get("nutrition_basis") or "口径未注明") + "）。")
    else:
        parts.append("保证值不完整，暂时无法精确比较蛋白、脂肪和纤维变化。")
    return " ".join(parts)


def build_current_summary(state, incremental=False):
    slots = state["slots"]
    def visible(key):
        return not incremental or key in state["analysis"].get("changed_fields", [])
    show_product = not incremental or state["analysis"].get("show_product_evidence")
    parts = [summarize_baseline_diet(state)] if show_product else []
    if slots.get("current_product") and visible("current_product"):
        parts.append("你确认当前主粮是：" + slots["current_product"] + "。")
    if slots.get("recent_diet_change") is False and visible("recent_diet_change"):
        parts.append("你确认近期没有饮食变化。")
    elif slots.get("recent_diet_change") is True and any(visible(k) for k in ("recent_diet_change", "old_product", "new_product", "added_food", "change_days", "change_ratio")):
        details = []
        if slots.get("main_food_changed") is True or slots.get("change_type") == "main_food_change":
            details.append("换主粮")
        if slots.get("added_food"):
            details.append("新加食物：" + slots["added_food"])
        if slots.get("old_product"):
            details.append("原粮：" + slots["old_product"])
        if slots.get("new_product"):
            details.append("新粮：" + slots["new_product"])
        if slots.get("change_days") is not None:
            details.append(f"开始变化约 {slots['change_days']} 天")
        if slots.get("change_ratio") is not None:
            details.append(f"新食物占比约 {slots['change_ratio'] * 100:g}%")
        parts.append("近期有饮食变化" + ("：" + "；".join(details) if details else "") + "。")
    for key, label in (("soft_stool_days", "软便持续时间"),):
        if slots.get(key) is not None and visible(key):
            parts.append("你确认软便今天开始。" if slots[key] == 0 else f"{label}约 {slots[key]} 天。")
    for key, label in (("vomiting", "呕吐"), ("blood_in_stool", "便血"), ("mucus_in_stool", "明显黏液")):
        if slots.get(key) is not None and visible(key):
            parts.append(("有" if slots[key] else "没有") + label + "。")
    for key, label in (("appetite", "食欲"), ("energy", "精神")):
        if slots.get(key) and visible(key):
            parts.append(label + ("下降。" if slots[key] == "poor" else "正常。"))
    for key, label in (("history_foods_reviewed", "历史饮食补充"), ("disease_history_reviewed", "疾病补充")):
        if slots.get(key) and visible(key):
            parts.append(label + "：" + slots[key] + "。")
    compare = state["analysis"].get("diet_compare") or {}
    if compare and (not incremental or any(visible(k) for k in ("old_product", "new_product"))):
        known = [f"{label}变化 {compare['delta'][key]:+g} 个百分点" for key, label in (
            ("protein", "蛋白"), ("fat", "脂肪"), ("fiber", "粗纤维"),
        ) if compare.get("delta", {}).get(key) is not None]
        parts.append("新旧粮标示保证值对比：" + ("；".join(known) if known else "保证值缺失或口径不同，无法计算营养差异") + "。")
    if state["analysis"].get("data_warnings") and show_product:
        parts.append("部分产品或历史资料暂不可用，以下分析只使用已取得的信息。")
    unknown = [key for key, meta in state["slot_status"].items() if meta["status"] in ("unknown", "deferred")]
    if unknown and not incremental:
        parts.append("有部分信息尚不确定，本轮按信息有限处理。")
    return " ".join(parts) or "这部分暂时没有补充资料，我会沿用已经确认的信息继续分析。"


def _has_urgent_risk(state):
    return policy.urgent_risk(state["slots"])


def choose_next_question_group(state):
    group, question, fields = policy.next_question(state)
    state["context"]["active_question_fields"] = fields
    return group, question


def run_required_tools(state):
    slots = state["slots"]
    analysis = state["analysis"]
    # A correction must not retain the previous product's comparison/mechanisms.
    analysis["diet_compare"] = None
    analysis["new_product_detail"] = None
    if slots.get("recent_diet_change") is not True:
        return
    main_change = slots.get("main_food_changed") is True or slots.get("change_type") == "main_food_change"
    if not main_change or not slots.get("new_product"):
        return
    refs = (slots.get("old_product"), slots["new_product"])
    cache = state["context"].setdefault("comparison_cache", {})
    cache_key = repr(refs)
    if cache_key not in cache:
        try:
            new = get_product_by_name(slots["new_product"])
            compare = build_manual_old_new_compare(*refs).get("compare") if refs[0] else None
            cache[cache_key] = {"new_product_detail": new, "diet_compare": compare}
            if not new:
                _warn(state, "new_product_not_found")
        except DATA_ERRORS:
            _warn(state, "comparison_unavailable")
            return
        # Bound JSON state across repeated product corrections.
        while len(cache) > 5:
            del cache[next(iter(cache))]
    analysis.update(cache[cache_key])


def build_assessment_plan(state):
    slots = state["slots"]
    if _has_urgent_risk(state):
        return {"risk": "high", "judgment": "已有需要优先评估的伴随表现，不能只按饮食适应解释。",
                "next_steps": ["尽快联系宠物医院，提供症状、饮食变化和已有病历信息。"],
                "observe": ["记录便便表现、呕吐、食欲和精神变化。"],
                "review": "优先由宠物医院评估，不继续普通配方追问。"}
    changed = slots.get("recent_diet_change")
    change_days, symptom_days = slots.get("change_days"), slots.get("soft_stool_days")
    if changed is True:
        if change_days is not None and symptom_days is not None and change_days < symptom_days:
            judgment = "软便早于这次饮食变化，不能用这次变化解释软便的起点。"
        elif change_days is not None and symptom_days is not None:
            judgment = "饮食变化先于或接近软便出现，时间上可能相关，但尚不能确定因果。"
        else:
            judgment = "近期有饮食变化，但时间关系尚不清楚，暂不能确定关联。"
        step = "先不继续增加新食物比例，记录原粮、新粮及罐头零食的变化，避免同时新增多种食物。"
    elif changed is False:
        judgment = "没有明确近期饮食变化，不能优先归因到换粮；先对照历史吃粮表现和已有健康背景。"
        step = "记录现有喂食与排便情况，避免同时引入新的食物，以便观察变化。"
    else:
        judgment = "当前配方只能提供参考，还需结合它以前吃粮的表现和已有健康背景，不能直接认定是这款粮造成软便。"
        step = "保持饮食记录，暂不同时添加多种新食物；对照以前吃得适应的粮，整理后续选粮的参考。"
    limited_products = any((p.get("soft_stool_mechanisms") or {}).get("confidence") == "limited" for p in (state["context"].get("baseline_diet") or {}).get("detailed_products", []))
    history = slots.get("history_foods_reviewed")
    if history and history != "没有其他主粮记录":
        judgment += " 以前吃粮的表现可作为个体耐受参考，不能只按配方评分选择。"
    if state["context"].get("disease_names") or (slots.get("disease_history_reviewed") and slots["disease_history_reviewed"] != "没有其他病历补充"):
        judgment += " 已有健康背景需要作为饮食选择的约束，不能单靠配方分析代替病历评估。"
    return {"risk": "limited" if limited_products or bool(state["analysis"].get("data_warnings")) or not (state["context"].get("baseline_diet") or {}).get("has_diet_data") or any(state["slot_status"].get(k, {}).get("status") in ("unknown", "deferred")
                                         for k in ("current_product", "history_foods_reviewed", "disease_history_reviewed")) else "routine",
            "judgment": judgment, "next_steps": [step],
            "observe": ["记录便便形态和次数，以及呕吐、食欲和精神表现；新出现的黏液也要记录。"],
            "review": "持续、加重或出现便血、呕吐、食欲下降、精神变差时，联系宠物医院评估；有新记录可继续这次咨询。"}


def build_final_assessment(state):
    plan = build_assessment_plan(state)
    return " ".join([plan["judgment"], *plan["next_steps"], *plan["observe"], plan["review"]])


def upgrade_state(state):
    if state.get("rule_version") != policy.RULE_VERSION:
        for key in ("soft_stool_days", "change_days"):
            if state.get("slots", {}).get(key) == 0 and not state.get("slot_status", {}).get(key, {}).get("source_text"):
                state["slots"][key] = None
                state.get("slot_status", {}).pop(key, None)
                state["summary"] = {}
                for cached in ("last_reasoning", "current_summary", "assessment_plan"):
                    state.get("analysis", {}).pop(cached, None)
    state["rule_version"] = policy.RULE_VERSION


def run_soft_stool_turn(pet_id, user_input, state=None, diet_override=None, prefill=True, extracted=None):
    defaults = new_state(pet_id)
    state = deepcopy(state) if state else defaults
    for key, value in defaults.items():
        state.setdefault(key, deepcopy(value))
    for key, value in defaults["context"].items():
        state["context"].setdefault(key, deepcopy(value))
    for key in policy.SLOT_DEFAULTS:
        state["slots"].setdefault(key, None)
    # v3 accepted model-default zero as confirmed; preserve all other facts.
    upgrade_state(state)
    if not state["turn"]:
        state["context"].update(diet_override=diet_override, prefill=prefill)
    state["turn"] += 1
    state["analysis"]["show_product_evidence"] = state["turn"] == 1
    update_slots(state, extract_user_info(user_input, state) if extracted is None else extracted)
    # No model generation or product/history queries before an urgent response.
    if _has_urgent_risk(state):
        summary = build_current_summary(state)
        plan = build_assessment_plan(state)
        state["analysis"].update(generation_source="rule", model_name=None, fallback_reason=None)
        parts = [{"response_type": "evidence_card", "reply": summary},
                 {"response_type": "risk_alert", "reply": build_final_assessment(state)}]
        group = None
    else:
        load_baseline_context(state)
        load_followup_context(state)
        run_required_tools(state)
        summary = build_current_summary(state)
        group, question = choose_next_question_group(state)
        # Ask each optional background step once; a non-answer never holds up
        # advice. Explicit answers or corrections can still arrive in later turns.
        while group and state["question_attempts"].get(group, 0) >= 1:
            for key in state["context"]["active_question_fields"]:
                state["slot_status"][key] = {"status": "deferred", "source": "unanswered_question", "turn": state["turn"]}
            group, question = choose_next_question_group(state)
        if group:
            state["question_attempts"][group] = state["question_attempts"].get(group, 0) + 1
        summary = build_current_summary(state)
        plan = build_assessment_plan(state)
        state["analysis"]["assessment_plan"] = plan
        state["analysis"]["next_group"] = group
        parts = generate_two_stage_response(state, build_current_summary(state, incremental=True), summary, question)
    state["context"]["active_question_group"] = group
    state["status"] = "active" if group else "answered"
    state["analysis"].update(current_summary=summary, assessment_plan=plan)
    reply = "\n\n".join(p["reply"] for p in parts)
    state["summary"] = {"facts": summary, "assessment": plan["judgment"], "rule_version": policy.RULE_VERSION}
    return {"status": "need_more_info" if group else "answered", "reply": reply,
            "reply_parts": parts, "pattern": state["pattern"], "next_group": group,
            "rule_version": policy.RULE_VERSION, "limited_info": plan["risk"] == "limited",
            "actions": ([{"type": "medical_record", "label": "补充病历", "pet_id": pet_id}]
                        if group == "medical_history_group" else
                        ([{"type": "flow_handoff", "target_flow": "food_switch", "label": "继续聊选粮", "message": "帮我选一款适合的主粮"}]
                         if not group and plan["risk"] != "high" else [])),
            "state": state}


soft_stool_pipeline = run_soft_stool_turn
