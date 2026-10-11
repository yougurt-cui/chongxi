"""Versioned consultation fields and question groups; no clinical scoring changes."""

import math
from services.guardian_workflow import Step, next_step

RULE_VERSION = "guardian-soft-stool-v4"
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


def _history_question(state, fields):
    return "以前吃过哪些主粮，吃的时候便便怎么样？没有其他记录也可以直接告诉我。"


def _medical_question(state, fields):
    diseases = state["context"].get("disease_names") or []
    prefix = "档案里已有" + "、".join(diseases)[:100] + "。" if diseases else ""
    return prefix + "之前有确诊疾病或相关病历需要补充吗？没有或暂时不清楚也没关系。"


def _change_question(state, fields):
    labels = {"old_product": "原来吃什么主粮", "new_product": "现在换成什么主粮",
              "added_food": "新加的食物是什么", "change_days": "这次饮食变化从什么时候开始"}
    return "；".join(labels[key] for key in fields) + "？"


def _main_change(state):
    slots = state["slots"]
    return slots.get("recent_diet_change") is True and (slots.get("main_food_changed") is True or slots.get("change_type") == "main_food_change")


STEPS = (
    Step("current_food_group", ("current_product",),
         lambda state, fields: "它现在主要吃哪款主粮？有品牌和具体名称的话，我可以先帮你看看配方。",
         lambda state: not (state["context"].get("baseline_diet") or {}).get("has_diet_data")),
    Step("diet_change_detail_group", ("old_product", "new_product", "change_days"), _change_question, _main_change),
    Step("added_food_detail_group", ("added_food", "change_days"), _change_question,
         lambda state: state["slots"].get("recent_diet_change") is True and not _main_change(state)),
    Step("history_food_group", ("history_foods_reviewed",), _history_question),
    Step("medical_history_group", ("disease_history_reviewed",), _medical_question),
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
    return next_step(state, STEPS, unanswered)
