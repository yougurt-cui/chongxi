"""Small, data-independent building blocks for guardian flows and handoffs.

Steps own entry conditions and questions; business tools and model prompts stay
in their respective flows. New cases can compose steps without changing storage.
"""

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Callable


@dataclass(frozen=True)
class Step:
    name: str
    fields: tuple[str, ...]
    question: Callable
    when: Callable = lambda state: True


@dataclass(frozen=True)
class FlowStrategy:
    """Function-backed strategy with a shared matches/handle contract."""
    name: str
    matches: Callable
    handle: Callable


def select_strategy(message, state, strategies):
    return next((strategy for strategy in strategies if strategy.matches(message, state)), None)


def next_step(state, steps, unanswered):
    for step in steps:
        if not step.when(state):
            continue
        missing = [field for field in step.fields if unanswered(state, field)]
        if missing:
            return step.name, step.question(state, missing), missing
    return None, None, []


# Explicit requests have priority over continuing a pending questionnaire.
# Add routes here as their handlers become available; do not invent workflows
# for unsupported symptoms merely because they resemble an existing case.
FLOW_REQUESTS = (
    ('reminder', re.compile(r'提醒我|提醒一下|设置.{0,8}提醒|设个.{0,8}提醒|(?:添加|创建|新增).{0,8}提醒|(?:每天|每周|每月|明天|明晚).{0,25}提醒')),
    ("food_switch", re.compile(r"(?:推荐|挑选|选择|选|挑|换).{0,16}(?:口粮|猫粮|狗粮|主粮|粮食|粮|品牌)|(?:粮|品牌).{0,10}(?:推荐|选哪|选什么)")),
)


def requested_flow(message):
    text = message or ""
    if re.search(r"(?:不|别|不用|不要|暂不|先不)(?:用|再|需要)?(?:推荐|选|挑|换)", text):
        return None
    # Questions about principles or care are knowledge requests, not an
    # instruction to start the personalized food-selection questionnaire.
    if (re.search(r"(?:选粮|换粮|挑粮).{0,12}(?:注意什么|注意事项|怎么观察|如何观察|攻略|原则|知识)", text)
            and not re.search(r"帮我|给我|替我|推荐一款|想换|准备换|打算换", text)):
        return None
    for name, pattern in FLOW_REQUESTS:
        if pattern.search(text):
            # Reporting a past change is a feeding fact, not a navigation request.
            if name == "food_switch" and not re.search(r"推荐|选|挑|想|打算|准备|考虑|能否|可以|帮我|品牌", text):
                continue
            return name
    return None


def build_handoff(pipeline, target):
    """A versioned snapshot with provenance, not a diagnosis or an inferred diet."""
    context = pipeline.get("context") or {}
    baseline = context.get("baseline_diet") or {}
    return deepcopy({
        "version": "guardian-context-v1", "pet_id": pipeline.get("pet_id"),
        "source_flow": pipeline.get("pipeline", "soft_stool"), "target_flow": target,
        "source_turn": pipeline.get("turn"), "rule_version": pipeline.get("rule_version"),
        "pet_profile": context.get("pet_profile") or {},
        "current_diet": baseline,
        "diet_intent_reference": context.get("diet_intent_reference") or {},
        "diet_history": context.get("diet_history") or [],
        "diet_association_note": context.get("diet_association_note"),
        "facts": pipeline.get("slots") or {},
        "fact_sources": pipeline.get("slot_status") or {},
        "summary": pipeline.get("summary") or {},
        "product_comparison": (pipeline.get("analysis") or {}).get("diet_compare"),
        "data_warnings": (pipeline.get("analysis") or {}).get("data_warnings") or [],
    })
