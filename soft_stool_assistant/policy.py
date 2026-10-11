"""Versioned consultation fields and question groups; no clinical scoring changes."""

import math

RULE_VERSION = "soft-stool-consultation-v3"
SLOT_DEFAULTS = dict.fromkeys((
    "recent_diet_change", "change_type", "main_food_changed", "wet_food_added",
    "snack_added", "other_food_added", "current_product", "old_product", "new_product",
    "added_food", "change_ratio", "change_days", "soft_stool_days",
    "vomiting", "blood_in_stool", "mucus_in_stool", "appetite", "energy",
    "history_foods_reviewed", "disease_history_reviewed",
))
BOOLEAN_FIELDS = {
    "recent_diet_change", "main_food_changed", "wet_food_added", "snack_added",
    "other_food_added", "vomiting", "blood_in_stool", "mucus_in_stool",
}
QUESTION_GROUPS = (
    ("recent_diet_change_group", ("recent_diet_change", "current_product"),
     {"recent_diet_change": "最近有没有换主粮，或添加罐头、零食等新食物", "current_product": "目前主要吃什么主粮"}),
    ("diet_change_detail_group", ("old_product", "new_product", "added_food", "change_days", "change_ratio"),
     {"old_product": "原来吃什么主粮", "new_product": "现在换成什么主粮",
      "added_food": "新加的食物是什么", "change_days": "从几天前开始变化",
      "change_ratio": "新食物大约占每天饮食的多少"}),
    ("symptom_group", ("soft_stool_days", "vomiting", "blood_in_stool", "mucus_in_stool"),
     {"soft_stool_days": "软便持续多久", "vomiting": "有没有呕吐",
      "blood_in_stool": "有没有便血", "mucus_in_stool": "有没有明显黏液"}),
    ("risk_group", ("appetite", "energy"),
     {"appetite": "食欲有没有下降", "energy": "精神有没有变差"}),
    ("history_and_disease_group", ("history_foods_reviewed", "disease_history_reviewed"),
     {"history_foods_reviewed": "以前吃过哪些主粮、吃后怎么样",
      "disease_history_reviewed": "有没有基础疾病或其他病历需要补充"}),
)


def validate_extraction(value):
    """Reject invented field types, bool-as-number and non-finite model output."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key not in SLOT_DEFAULTS or item is None:
            continue
        if key in BOOLEAN_FIELDS:
            if type(item) is bool:
                result[key] = item
        elif key in {"change_days", "soft_stool_days"}:
            if type(item) is int and 0 <= item <= 36500:
                result[key] = item
        elif key == "change_ratio":
            if type(item) in (int, float) and math.isfinite(item) and 0 <= item <= 1:
                result[key] = float(item)
        elif key in {"appetite", "energy"}:
            if item in ("normal", "poor"):
                result[key] = item
        elif key == "change_type":
            if item in ("main_food_change", "wet_food_added", "snack_added", "other_food_added", "multiple"):
                result[key] = item
        elif isinstance(item, str) and item.strip():
            result[key] = item.strip()[:1000]
    unknown = value.get("unknown_fields")
    if isinstance(unknown, list):
        result["unknown_fields"] = [k for k in unknown if isinstance(k, str) and k in SLOT_DEFAULTS and k not in result]
    return result


def urgent_risk(slots):
    # Keep the existing policy; mucus is collected independently from blood.
    return bool(slots.get("vomiting") is True or slots.get("blood_in_stool") is True
                or slots.get("appetite") == "poor" or slots.get("energy") == "poor")


def unanswered(state, key):
    return state["slots"].get(key) is None and state.get("slot_status", {}).get(key, {}).get("status") not in ("unknown", "deferred")


def next_question(state):
    slots = state["slots"]
    for group, keys, labels in QUESTION_GROUPS:
        if group == "recent_diet_change_group" and (state["context"].get("baseline_diet") or {}).get("has_diet_data"):
            keys = ("recent_diet_change",)
        if group == "diet_change_detail_group":
            if slots.get("recent_diet_change") is not True:
                continue
            main_change = slots.get("main_food_changed") is True or slots.get("change_type") == "main_food_change"
            keys = tuple(k for k in keys if (
                k in ("change_days", "change_ratio")
                or (main_change and k in ("old_product", "new_product"))
                or (k == "added_food" and (not main_change or any(slots.get(f) is True for f in ("wet_food_added", "snack_added", "other_food_added"))))
            ))
        missing = [key for key in keys if unanswered(state, key)]
        if missing:
            question = "；".join(labels[key] for key in missing) + "？"
            if group == "history_and_disease_group":
                diseases = state["context"].get("disease_names") or []
                if diseases:
                    question = "档案已有" + "、".join(diseases)[:100] + "。" + question
            return group, question, missing
    return None, None, []
