"""Persistent pet-assistant chat orchestration for the WeChat mini-program."""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import pymysql
from openai import OpenAI

from app_config import get_chat_model_config, get_mysql_config
from services.miniprogram_cat_profile_service import get_cat_profile, list_cat_profiles
from services.miniprogram_food_change_service import (
    get_catalog_product_ingredients,
    list_standard_product_candidates,
)
from services.miniprogram_food_submission_service import get_food_submission, list_food_submissions
from services.miniprogram_diet_history_service import list_diet_history, compact_diet_context, list_diet_references
from services.guardian_recommendation_service import recommend_guardian_foods, explicit_preferences, recommendation_goal
from services.guardian_food_flow_service import handle_food_flow, STATE_KEY as FOOD_FLOW_STATE_KEY
from services.guardian_food_goals import GOALS, goals_in_text, goal_options
from services.guardian_food_analysis_service import recommendation_text
from services.guardian_food_scenario import (extract_health, build_scenario, scenario_question, STATES, STAGES)
from services.guardian_product_portrait_service import load_product_portraits, compare_portraits, portrait_comparison_text
from services.guardian_reminder_flow_service import handle_reminder
from services.soft_stool_assistant_service import handle_turn as handle_soft_stool_turn
from services.guardian_workflow import FlowStrategy, select_strategy, requested_flow, build_handoff


CONVERSATION_TABLE = "miniprogram_chat_conversation"
STATE_TABLE = "miniprogram_chat_state"
MESSAGE_TABLE = "miniprogram_chat_message"
PROMPT_VERSION = "pet-manager-chat-v2"
MAX_MESSAGE_LENGTH = 4000
SOFT_STOOL_STATE_SLOT = "_soft_stool_pipeline_state"
GUARDIAN_CONTEXT_SLOT = "_guardian_context"
SOFT_STOOL_TRIGGER_RE = re.compile(r"软便|便软|稀便|拉稀|腹泻")

VALID_INTENTS = {
    "symptom_consult", "food_switch", "food_analysis",
    "ingredient_analysis", "general_pet_qa", 'reminder',
}

INTENT_SLOTS = {
    'reminder': {'title', 'date', 'time', 'repeat'},
    "symptom_consult": {
        "symptom", "duration_days", "frequency_per_day", "severity", "vomiting",
        "blood_in_stool", "mental_status", "appetite_status", "warning_signs",
        "recent_food_change", "symptom_context",
    },
    "food_switch": {"current_food", "switch_reason", "target_food", "target_requirement", "recent_symptom",
                    "budget_per_jin", "preferred_brand", "origin_type", "food_goal"},
    "food_analysis": {"food_name", "catalog_key"},
    "ingredient_analysis": {"ingredient_name", "food_name", "ingredient_source"},
    "general_pet_qa": {"topic"},
}

FLOW_CONFIG = {
    "symptom_consult": {
        "required": ["symptom"],
        "priority": [],
        "max_followups": 0,
    },
    "food_switch": {
        "required": ["current_food", "switch_reason", "target_requirement"],
        "priority": ["switch_reason", "target_requirement", "current_food"],
        "max_followups": 2,
    },
    "food_analysis": {"required": ["food_name"], "priority": ["food_name"], "max_followups": 1},
    "ingredient_analysis": {"required": ["ingredient_source"], "priority": ["ingredient_source"], "max_followups": 1},
    "general_pet_qa": {"required": [], "priority": [], "max_followups": 0},
}

SLOT_QUESTIONS = {
    "warning_signs": {
        "text": "听起来它现在有点不舒服，我先陪你一起理一理。除了刚才说的情况，还有没有呕吐、便血、精神明显变差或不太想吃东西呢？", "response_type": "text",
        "options": [],
    },
    "severity": {
        "text": "我再确认一下，它现在看起来是轻微不舒服、比较明显，还是已经很严重了呢？", "response_type": "single_select",
        "options": [{"label": "轻微", "value": "mild"}, {"label": "比较明显", "value": "moderate"}, {"label": "很严重", "value": "severe"}],
    },
    "duration_days": {
        "text": "这种情况大概持续多久了呢？", "response_type": "single_select",
        "options": [{"label": "今天开始", "value": 1}, {"label": "2～3天", "value": 3}, {"label": "超过3天", "value": 4}],
    },
    "recent_food_change": {
        "text": "最近有没有刚换粮、加罐头，或者吃新的零食呀？", "response_type": "single_select",
        "options": [{"label": "没有", "value": False}, {"label": "有", "value": True}],
    },
    "symptom_context": {"text": "好的，了解了。这种情况大概持续多久了？最近有没有刚换粮、加罐头或者吃新的零食呀？", "response_type": "text", "options": []},
    "current_food": {"text": "可以告诉我它现在主要吃哪一款食品吗？", "response_type": "food_select", "options": []},
    "switch_reason": {
        "text": "想给它换得更合适一些，对吧？这次主要是因为肠胃、皮肤、体重，还是单纯想换一款呢？", "response_type": "single_select",
        "options": [
            {"label": "软便/肠胃问题", "value": "digestive"}, {"label": "皮肤/掉毛", "value": "skin"},
            {"label": "体重管理", "value": "weight"}, {"label": "怀疑食物不耐受", "value": "intolerance"},
            {"label": "单纯想换一款", "value": "general"},
        ],
    },
    "target_requirement": {"text": "明白啦，那你更希望下一款粮在哪方面更贴合它呢？", "response_type": "text_or_select", "options": []},
    "food_name": {"text": "可以呀，把食品名称告诉我就好；如果手边有配料表照片，也可以直接发给我。", "response_type": "food_select", "options": []},
    "ingredient_source": {"text": "可以把食品名称告诉我，或者直接上传配料表照片，我来帮你一起看看。", "response_type": "ingredient_source", "options": []},
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect(autocommit: bool = False):
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=autocommit)


def _clean(value: Any, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    return text[:max_length] if max_length else text


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return deepcopy(default)
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return deepcopy(default)


def init_chat_tables() -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {CONVERSATION_TABLE} (
                    id CHAR(32) NOT NULL,user_id VARCHAR(128) NOT NULL,pet_id CHAR(32) NULL,
                    title VARCHAR(255) NULL,scene VARCHAR(32) NOT NULL DEFAULT 'pet_manager',
                    primary_intent VARCHAR(32) NULL,status VARCHAR(16) NOT NULL DEFAULT 'active',
                    last_message_at DATETIME NULL,created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,
                    PRIMARY KEY(id),KEY idx_chat_user(user_id,status,last_message_at),KEY idx_chat_pet(pet_id,status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                    conversation_id CHAR(32) NOT NULL,primary_intent VARCHAR(32) NULL,
                    secondary_intent VARCHAR(32) NULL,slots_json LONGTEXT NULL,missing_slots_json LONGTEXT NULL,
                    current_step VARCHAR(64) NULL,risk_level VARCHAR(16) NOT NULL DEFAULT 'unknown',
                    turn_count INT NOT NULL DEFAULT 0,followup_count INT NOT NULL DEFAULT 0,updated_at DATETIME NOT NULL,
                    PRIMARY KEY(conversation_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {MESSAGE_TABLE} (
                    id CHAR(32) NOT NULL,conversation_id CHAR(32) NOT NULL,user_id VARCHAR(128) NOT NULL,
                    role VARCHAR(16) NOT NULL,content TEXT NULL,response_type VARCHAR(32) NULL,
                    interaction_json LONGTEXT NULL,attachments_json LONGTEXT NULL,result_json LONGTEXT NULL,
                    model_name VARCHAR(128) NULL,prompt_version VARCHAR(64) NULL,status VARCHAR(16) NOT NULL DEFAULT 'active',
                    created_at DATETIME NOT NULL,PRIMARY KEY(id),
                    KEY idx_chat_message_conversation(conversation_id,created_at),KEY idx_chat_message_user(user_id,created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """)
        conn.commit()


def _default_state(conversation_id: str) -> dict[str, Any]:
    return {"conversation_id": conversation_id, "primary_intent": None, "secondary_intent": None,
            "slots": {}, "missing_slots": [], "current_step": None, "risk_level": "unknown",
            "turn_count": 0, "followup_count": 0}


def _get_conversation(user_id: str, conversation_id: str) -> dict[str, Any]:
    init_chat_tables()
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {CONVERSATION_TABLE} WHERE id=%s AND user_id=%s AND status='active' LIMIT 1", (conversation_id, user_id))
            row = cursor.fetchone()
    if not row:
        raise LookupError("会话不存在")
    return row


def _load_state(conversation_id: str) -> dict[str, Any]:
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {STATE_TABLE} WHERE conversation_id=%s", (conversation_id,))
            row = cursor.fetchone()
    if not row:
        return _default_state(conversation_id)
    return {"conversation_id": conversation_id, "primary_intent": row.get("primary_intent"),
            "secondary_intent": row.get("secondary_intent"), "slots": _json_loads(row.get("slots_json"), {}),
            "missing_slots": _json_loads(row.get("missing_slots_json"), []), "current_step": row.get("current_step"),
            "risk_level": row.get("risk_level") or "unknown", "turn_count": int(row.get("turn_count") or 0),
            "followup_count": int(row.get("followup_count") or 0)}


def _save_state(state: dict[str, Any]) -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"""INSERT INTO {STATE_TABLE}
                (conversation_id,primary_intent,secondary_intent,slots_json,missing_slots_json,current_step,
                 risk_level,turn_count,followup_count,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE primary_intent=VALUES(primary_intent),secondary_intent=VALUES(secondary_intent),
                slots_json=VALUES(slots_json),missing_slots_json=VALUES(missing_slots_json),current_step=VALUES(current_step),
                risk_level=VALUES(risk_level),turn_count=VALUES(turn_count),followup_count=VALUES(followup_count),updated_at=VALUES(updated_at)""",
                (state["conversation_id"], state.get("primary_intent"), state.get("secondary_intent"),
                 _json_dumps(state.get("slots") or {}), _json_dumps(state.get("missing_slots") or []),
                 state.get("current_step"), state.get("risk_level") or "unknown", state.get("turn_count", 0),
                 state.get("followup_count", 0), _now()))
        conn.commit()


def _save_message(*, conversation_id: str, user_id: str, role: str, content: str,
                  response_type: str | None = None, interaction: Any = None, attachments: Any = None,
                  result: Any = None, model_name: str | None = None) -> dict[str, Any]:
    message_id, now = uuid.uuid4().hex, _now()
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"""INSERT INTO {MESSAGE_TABLE}
                (id,conversation_id,user_id,role,content,response_type,interaction_json,attachments_json,
                 result_json,model_name,prompt_version,status,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)""",
                (message_id, conversation_id, user_id, role, content or None, response_type,
                 _json_dumps(interaction) if interaction is not None else None,
                 _json_dumps(attachments) if attachments is not None else None,
                 _json_dumps(result) if result is not None else None, model_name, PROMPT_VERSION, now))
            cursor.execute(f"UPDATE {CONVERSATION_TABLE} SET last_message_at=%s,updated_at=%s WHERE id=%s AND user_id=%s", (now, now, conversation_id, user_id))
        conn.commit()
    return {"id": message_id, "conversation_id": conversation_id, "role": role, "content": content,
            "response_type": response_type, "interaction": interaction, "attachments": attachments,
            "result": result, "created_at": now}


def create_conversation(user_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    user_id = _clean(user_id, 128)
    pet_id = _clean(payload.get("pet_id"), 32)
    if not pet_id:
        profiles = list_cat_profiles(user_id, limit=1).get("items") or []
        if not profiles:
            raise ValueError("请先创建宠物档案")
        pet_id = profiles[0]["id"]
    pet = get_cat_profile(user_id, pet_id)
    init_chat_tables()
    conversation_id, now = uuid.uuid4().hex, _now()
    title = _clean(payload.get("title"), 255) or f"{pet.get('name') or '宠物'}的宠物管家"
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"INSERT INTO {CONVERSATION_TABLE} (id,user_id,pet_id,title,scene,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,'active',%s,%s)",
                           (conversation_id, user_id, pet_id, title, _clean(payload.get("scene"),32) or "pet_manager", now, now))
        conn.commit()
    _save_state(_default_state(conversation_id))
    return {"ok": True, "conversation": {"id": conversation_id, "pet_id": pet_id, "title": title, "created_at": now},
            "welcome": {"response_type": "welcome", "reply": f"嗨，我是{pet.get('name') or '它'}的宠物管家，今天想聊点什么？"}}


def list_conversations(user_id: Any, *, limit: Any = 50) -> dict[str, Any]:
    init_chat_tables()
    limit = max(1, min(int(limit or 50), 100))
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT id,pet_id,title,primary_intent,last_message_at,created_at,updated_at FROM {CONVERSATION_TABLE} WHERE user_id=%s AND status='active' ORDER BY COALESCE(last_message_at,created_at) DESC LIMIT %s", (_clean(user_id,128),limit))
            rows = list(cursor.fetchall() or [])
    return {"ok": True, "count": len(rows), "items": [{**row, "created_at": str(row.get("created_at") or ""), "updated_at": str(row.get("updated_at") or ""), "last_message_at": str(row.get("last_message_at") or "")} for row in rows]}


def list_messages(user_id: Any, conversation_id: Any, *, limit: Any = 100) -> dict[str, Any]:
    user_id, conversation_id = _clean(user_id,128), _clean(conversation_id,32)
    _get_conversation(user_id, conversation_id)
    limit = max(1, min(int(limit or 100), 200))
    with _connect(autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"""SELECT * FROM {MESSAGE_TABLE} WHERE conversation_id=%s AND user_id=%s AND status='active' ORDER BY created_at ASC,
                CAST(COALESCE(CASE WHEN JSON_VALID(result_json) THEN JSON_EXTRACT(result_json, '$.turn_no') END, 0) AS UNSIGNED),
                CAST(COALESCE(CASE WHEN JSON_VALID(result_json) THEN JSON_EXTRACT(result_json, '$.sequence_no') END, 0) AS UNSIGNED), id ASC LIMIT %s""", (conversation_id,user_id,limit))
            rows = list(cursor.fetchall() or [])
    items = [{"id": row["id"], "role": row["role"], "content": row.get("content") or "",
              "response_type": row.get("response_type"), "interaction": _json_loads(row.get("interaction_json"), None),
              "attachments": _json_loads(row.get("attachments_json"), []), "result": _json_loads(row.get("result_json"), None),
              "created_at": str(row.get("created_at") or "")} for row in rows]
    return {"ok": True, "count": len(items), "items": items}


def delete_conversation(user_id: Any, conversation_id: Any) -> dict[str, Any]:
    user_id, conversation_id = _clean(user_id,128), _clean(conversation_id,32)
    _get_conversation(user_id, conversation_id)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE {CONVERSATION_TABLE} SET status='deleted',updated_at=%s WHERE id=%s AND user_id=%s", (_now(),conversation_id,user_id))
        conn.commit()
    return {"ok": True, "id": conversation_id}


def _parse_model_json(raw: str) -> dict[str, Any]:
    text = _clean(raw)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        value = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    return value if isinstance(value, dict) else {}


def _fallback_extraction(message: str, state: dict[str, Any]) -> dict[str, Any]:
    if any(word in message for word in ("软便", "腹泻", "呕吐", "便血", "没精神", "食欲")):
        symptom = next((word for word in ("软便", "腹泻", "呕吐", "便血") if word in message), "不适")
        from soft_stool_assistant.llm import _extract_rule_info
        explicit = _extract_rule_info(message, {})
        warning = [name for name, present in (
            ("blood_in_stool", explicit.get("blood_in_stool") is True),
            ("poor_mental_status", explicit.get("energy") == "poor"),
        ) if present]
        slots = {"symptom": symptom}
        if warning:
            slots["warning_signs"] = warning
        return {"primary_intent": "symptom_consult", "secondary_intent": "food_switch" if "换粮" in message else None, "slots": slots, "confidence": 0.7}
    if any(word in message for word in ("换粮", "低敏粮", "换猫粮")):
        return {"primary_intent": "food_switch", "secondary_intent": None, "slots": {}, "confidence": 0.7}
    if "配料" in message or "原料" in message:
        return {"primary_intent": "ingredient_analysis", "secondary_intent": None, "slots": {}, "confidence": 0.6}
    return {"primary_intent": state.get("primary_intent") or "general_pet_qa", "secondary_intent": None, "slots": {"topic": message[:100]}, "confidence": 0.5}


def _provider_options(cfg: dict[str, str]) -> dict[str, Any]:
    if cfg.get("provider") == "deepseek":
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def _extract_intent(message: str, state: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    cfg = get_chat_model_config()
    if not cfg["api_key"]:
        return _fallback_extraction(message, state), None
    prompt = {"allowed_intents": sorted(VALID_INTENTS), "current_intent": state.get("primary_intent"),
              "current_step": state.get("current_step"), "current_slots": {key: value for key, value in (state.get("slots") or {}).items() if not key.startswith("_")}, "latest_message": message,
              "output": {"primary_intent": "", "secondary_intent": None, "slots": {}, "confidence": 0.0}}
    try:
        response = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=45).chat.completions.create(
            model=cfg["model"], temperature=0, response_format={"type":"json_object"}, messages=[
                {"role":"system","content":"你是宠物咨询的意图与信息抽取模块。只提取用户明确表达的信息，不回答问题，只输出JSON。选粮slots可包含budget_per_jin（明确每斤预算才填数字，不能把每袋预算当每斤）、preferred_brand（明确品牌）、origin_type（domestic/imported）。未说明不要猜测。"},
                {"role":"user","content":_json_dumps(prompt)},
            ], **_provider_options(cfg))
        parsed = _parse_model_json(response.choices[0].message.content or "")
        intent = parsed.get("primary_intent")
        if intent not in VALID_INTENTS:
            intent = state.get("primary_intent") or "general_pet_qa"
        slots = {key:value for key,value in (parsed.get("slots") or {}).items() if key in INTENT_SLOTS[intent] and value is not None}
        return {"primary_intent":intent,"secondary_intent":parsed.get("secondary_intent"),"slots":slots,"confidence":parsed.get("confidence",0)}, cfg["model"]
    except Exception:
        return _fallback_extraction(message, state), None


def _warning_signs_from_text(message: str) -> list[str]:
    text = _clean(message)
    if any(word in text for word in ("都没有", "没有这些", "没有", "无")) and not any(
        word in text for word in ("便血", "血便", "呕吐", "吐了", "没精神", "精神差", "不吃", "食欲差")
    ):
        return ["none"]
    mapping = {
        "vomiting": ("呕吐", "吐了", "吐过"),
        "blood_in_stool": ("便血", "血便", "带血"),
        "poor_mental_status": ("没精神", "精神差", "精神不好", "精神变差"),
        "poor_appetite": ("不吃", "食欲差", "食欲下降", "没胃口"),
    }
    found = [code for code, words in mapping.items() if any(word in text for word in words)]
    return found or ["unclear"]


SOFT_STOOL_PAUSE_RE = re.compile(r"(?:不|别|停止|暂停)(?:聊|说|谈).{0,8}软便|换个话题|聊别的|先不聊(?:了)?")
SOFT_STOOL_OTHER_TOPIC_RE = re.compile(r"(?:先|改为|转而|换成)(?:帮我|聊|看看|分析).*(?:玩具|图片|配料|食品分析)|(?:帮我|推荐|看看).*(?:玩具|宠物日历)|(?:先|帮我).*(?:分析|看看|看下).*(?:配料表|食品分析)")
SOFT_STOOL_FOLLOWUP_RE = re.compile(r"继续|接下来|下一步|复评|建议|食欲|精神|便血|黏液|粘液|呕吐|软便|排便")


def _should_route_soft_stool(message: str, state: dict[str, Any]) -> bool:
    text = message or ""
    pipeline = (state.get("slots") or {}).get(SOFT_STOOL_STATE_SLOT)
    if pipeline or SOFT_STOOL_TRIGGER_RE.search(text):
        from soft_stool_assistant.llm import _extract_rule_info
        from soft_stool_assistant.policy import urgent_risk
        if urgent_risk(_extract_rule_info(text, pipeline or {})):
            return True
    if SOFT_STOOL_PAUSE_RE.search(text) or SOFT_STOOL_OTHER_TOPIC_RE.search(text):
        return False
    if requested_flow(text) == "food_switch":
        return False
    if SOFT_STOOL_TRIGGER_RE.search(text):
        return True
    if not pipeline or pipeline.get("status") == "paused":
        return False
    if pipeline.get("status") == "answered":
        return bool(SOFT_STOOL_FOLLOWUP_RE.search(text))
    return True


def _pause_soft_stool(state):
    pipeline = state.get("slots", {}).get(SOFT_STOOL_STATE_SLOT)
    if not pipeline or pipeline.get("status") == "paused":
        return
    pipeline["status"] = "paused"
    state.update(primary_intent=None, secondary_intent=None, current_step=None,
                 missing_slots=[], followup_count=0)
    # Leave the nested consultation available to resume, but clear generic routing slots.
    state["slots"].pop("symptom", None)


def _handle_soft_stool_message(
    user_id: str,
    conversation: dict[str, Any],
    message: str,
    state: dict[str, Any],
    source_message_id: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    pipeline_state = (state.get("slots") or {}).get(SOFT_STOOL_STATE_SLOT)
    baseline_source = (
        ((pipeline_state or {}).get("context") or {}).get("baseline_diet") or {}
    ).get("source")
    if baseline_source == "food_change_intent.previous_food" or (
        (pipeline_state or {}).get("status") == "answered"
        and re.search(r"(?:又|再次|重新).{0,8}(?:软便|拉稀|腹泻)", message)
    ):
        pipeline_state = None
    if pipeline_state and pipeline_state.get("status") == "paused":
        pipeline_state = deepcopy(pipeline_state)
        pending = (pipeline_state.get("context") or {}).get("active_question_group")
        # Changing topics is not a failed answer to the old question. Resume
        # the pending background step, without reopening completed steps.
        if pending:
            pipeline_state.get("question_attempts", {}).pop(pending, None)
    turn_payload = {
        "pet_id": conversation.get("pet_id"),
        "message": message,
        "state": pipeline_state,
    }
    recognized = kwargs.get("intent_result")
    extracted = recognized[0].get("_soft_extraction") if recognized else None
    result = handle_soft_stool_turn(user_id, turn_payload, **({"extracted": extracted} if extracted is not None else {}))
    if source_message_id:
        for meta in result["state"].get("slot_status", {}).values():
            if meta.get("turn") == result["state"].get("turn"):
                meta["message_id"] = source_message_id
    state["primary_intent"] = "symptom_consult"
    state["secondary_intent"] = None
    state["slots"]["symptom"] = "软便"
    state["turn_count"] += 1
    state["current_step"] = result.get("next_group")
    state["missing_slots"] = [result["next_group"]] if result.get("next_group") else []
    # Keep completed summaries for scoped follow-ups; routing no longer treats all saved states as active.
    state["slots"][SOFT_STOOL_STATE_SLOT] = result["state"]
    state["slots"][SOFT_STOOL_STATE_SLOT]["status"] = "active" if result.get("status") == "need_more_info" else "answered"
    state["risk_level"] = "high" if any(part.get("response_type") == "risk_alert" for part in result.get("reply_parts", [])) else "unknown"
    result_meta = {
        "pipeline": "soft_stool",
        "status": result.get("status"),
        "pattern": result.get("pattern"),
        "next_group": result.get("next_group"),
        "disclaimer": result.get("disclaimer"),
        "rule_version": result.get("rule_version"),
        "turn_no": state["turn_count"],
        "generation_source": result["state"].get("analysis", {}).get("generation_source", "fallback"),
        "model_name": result["state"].get("analysis", {}).get("model_name"),
        "fallback_reason": result["state"].get("analysis", {}).get("fallback_reason"),
        "limited_info": result.get("limited_info", False),
        "actions": result.get("actions") or [],
    }
    parts = result.get("reply_parts") if isinstance(result.get("reply_parts"), list) else []
    messages = [
        {
            "response_type": _clean(part.get("response_type"), 32) or "text",
            "reply": _clean(part.get("reply"), MAX_MESSAGE_LENGTH),
            "result": {**result_meta, "sequence_no": index, "message_kind": part.get("response_type"),
                       "limited_info": bool(result_meta["limited_info"] and result_meta["status"] == "answered" and index == 2),
                       "actions": result_meta["actions"] if index == 2 else []},
        }
        for index, part in enumerate(parts, 1)
        if isinstance(part, dict) and _clean(part.get("reply"), MAX_MESSAGE_LENGTH)
    ]
    if not messages:
        messages = [{
            "response_type": "text" if result.get("status") == "need_more_info" else "result_card",
            "reply": result["reply"],
            "result": result_meta,
        }]
    return {
        "response_type": messages[-1]["response_type"],
        "reply": result["reply"],
        "result": result_meta,
        "messages": messages,
        "disclaimer": result.get("disclaimer"),
    }


def _share_guardian_context(state, pet_id, target, extracted=None, source_message_id=None):
    nested = state.get("slots", {}).get(SOFT_STOOL_STATE_SLOT)
    if not nested or nested.get("pet_id") != pet_id:
        return None
    from soft_stool_assistant.pipeline import upgrade_state
    nested = deepcopy(nested)
    upgrade_state(nested)
    state["slots"][SOFT_STOOL_STATE_SLOT] = nested
    from soft_stool_assistant.policy import SLOT_DEFAULTS, validate_extraction
    values = validate_extraction(extracted) if extracted else {}
    has_facts = any(key in SLOT_DEFAULTS for key in values) or bool(values.get("unknown_fields"))
    if has_facts:
        from soft_stool_assistant.pipeline import update_slots, load_baseline_context, build_current_summary, run_required_tools, build_assessment_plan
        nested = deepcopy(nested)
        nested["turn"] = nested.get("turn", 0) + 1
        update_slots(nested, extracted)
        if not nested["context"].get("baseline_loaded"):
            load_baseline_context(nested)
        if nested["analysis"].get("changed_fields"):
            run_required_tools(nested)
        for meta in nested.get("slot_status", {}).values():
            if meta.get("turn") == nested["turn"]:
                meta["message_id"] = source_message_id
        nested.setdefault("summary", {})["facts"] = build_current_summary(nested)
        nested["summary"]["assessment"] = build_assessment_plan(nested)["judgment"]
        state["slots"][SOFT_STOOL_STATE_SLOT] = nested
    packet = build_handoff(nested, target)
    state["slots"][GUARDIAN_CONTEXT_SLOT] = packet
    return packet


def _prepare_food_handoff(state, pet_id, extracted=None, source_message_id=None):
    if state.get("primary_intent") == "food_switch":
        return
    packet = _share_guardian_context(state, pet_id, "food_switch", extracted, source_message_id)
    if not packet:
        return
    _pause_soft_stool(state)
    state["primary_intent"] = "food_switch"
    # Intentions remain references, never prefill a previous food as current.
    products = packet["current_diet"].get("detailed_products") or packet["current_diet"].get("products") or []
    current = packet["facts"].get("current_product") or "、".join(
        str(p.get("display_name") or p.get("name") or "") for p in products[:5]
    )
    if current:
        state["slots"]["current_food"] = current
    state["slots"]["switch_reason"] = "digestive"
    # Carry the known consultation topic, but do not infer its current phase.
    state['slots'].setdefault('food_health_state', 'soft_stool')


def _normalize_daily_records(value: Any) -> list[dict[str, Any]]:
    records = value if isinstance(value, list) else []
    result = []
    for item in records[:7]:
        if not isinstance(item, dict):
            continue
        notes = item.get("litter_notes") if isinstance(item.get("litter_notes"), list) else []
        result.append({
            "day": _clean(item.get("day"), 10),
            "water_ml": item.get("water_ml"),
            "food_g": item.get("food_g"),
            "stool_count": item.get("stool_count"),
            "stool_notes": [
                {"time": _clean(note.get("time"), 5), "count": note.get("count"), "shape": _clean(note.get("shape"), 60)}
                for note in notes[:10] if isinstance(note, dict)
            ],
        })
    return result


CURRENT_FOOD_ANALYSIS_RE = re.compile(r'(?:当前|现在|目前|正在).{0,8}(?:吃|喂|用)?.{0,6}(?:粮|主粮|食品)')
HISTORY_FOOD_ANALYSIS_RE = re.compile(r'(?:历史|以前|之前|过去|吃过|喂过).{0,8}(?:粮|主粮|食品)')
FOOD_COMPARISON_RE = re.compile(r'对比|比较|区别|差异|哪个好|哪款好|更适合')


def _food_analysis_by_formula(formula_id: Any, current_formula_id: Any = None) -> dict[str, Any] | None:
    """Load one verified standard formula and its portrait without name matching."""
    try:
        formula_id = int(formula_id)
        analysis = get_catalog_product_ingredients({'catalog_key': f'formula:{formula_id}'})
        portraits = load_product_portraits([formula_id, current_formula_id])
        analysis['product_portrait'] = portraits.get(formula_id)
        if current_formula_id and int(current_formula_id) != formula_id:
            analysis['portrait_comparison'] = compare_portraits(
                portraits.get(int(current_formula_id)), portraits.get(formula_id))
        return analysis
    except Exception:
        return None


def _prepare_saved_food_analysis(message: str, state: dict[str, Any], context: dict[str, Any]) -> None:
    """Resolve references such as 'current food' and 'foods eaten before' from saved pet data."""
    if state.get('primary_intent') != 'food_analysis':
        return
    slots = state.setdefault('slots', {})
    current = context.get('current_food') or {}
    history = context.get('diet_history') or []
    current_formula_id = current.get('formula_id')
    mentions_current = bool(CURRENT_FOOD_ANALYSIS_RE.search(message))
    mentions_history = bool(HISTORY_FOOD_ANALYSIS_RE.search(message))

    if mentions_current and mentions_history and FOOD_COMPARISON_RE.search(message):
        records = [item for item in history if not item.get('is_current')]
        context['food_analysis_scope'] = 'current_vs_history'
        context['food_analysis'] = (
            _food_analysis_by_formula(current_formula_id, current_formula_id)
            if current_formula_id else None
        )
        context['food_history_analyses'] = []
        for item in records[:10]:
            analysis = _food_analysis_by_formula(item.get('formula_id'), current_formula_id) if item.get('formula_id') else None
            context['food_history_analyses'].append({
                'diet_record': item,
                'analysis_status': 'available' if analysis else 'unavailable',
                'food_analysis': analysis,
            })
        if current and records:
            slots['food_name'] = '当前粮与历史口粮'
        if current_formula_id:
            slots['catalog_key'] = f'formula:{int(current_formula_id)}'
        return

    if mentions_history:
        records = [item for item in history if not item.get('is_current')]
        context['food_analysis_scope'] = 'history'
        context['food_history_analyses'] = []
        for item in records[:10]:
            analysis = _food_analysis_by_formula(item.get('formula_id'), current_formula_id) if item.get('formula_id') else None
            context['food_history_analyses'].append({
                'diet_record': item,
                'analysis_status': 'available' if analysis else 'unavailable',
                'food_analysis': analysis,
            })
        if records:
            slots['food_name'] = '历史口粮'
        return

    if mentions_current and current:
        context['food_analysis_scope'] = 'current'
        slots['food_name'] = ' '.join(filter(None, (current.get('brand'), current.get('product_name'))))
        if current_formula_id:
            slots['catalog_key'] = f'formula:{int(current_formula_id)}'
            context['food_analysis'] = _food_analysis_by_formula(current_formula_id, current_formula_id)


def _saved_food_comparison_text(context: dict[str, Any]) -> str | None:
    """Render verified portrait comparisons without allowing the model to invent missing dimensions."""
    if context.get('food_analysis_scope') != 'current_vs_history':
        return None
    current = context.get('current_food') or {}
    current_name = ' '.join(filter(None, (current.get('brand'), current.get('product_name')))) or '当前粮'
    sections = []
    for item in context.get('food_history_analyses') or []:
        record = item.get('diet_record') or {}
        history_name = ' '.join(filter(None, (record.get('brand'), record.get('product') or record.get('product_name')))) or '历史粮'
        comparison = (item.get('food_analysis') or {}).get('portrait_comparison')
        if not comparison:
            sections.append(f'{history_name}：缺少有效的当前版本画像，暂不能与{current_name}比较。')
            continue
        detail = portrait_comparison_text(comparison).replace('目标粮', '历史粮').replace('原粮', '当前粮')
        sections.append(f'{current_name} vs {history_name}\n{detail}')
    return '\n\n'.join(sections) if sections else '没有可用于对比的历史口粮记录。'


def _build_context(user_id: str, conversation: dict[str, Any], state: dict[str, Any],
                   attachments: list[dict[str, Any]], client_context: dict[str, Any] | None = None) -> dict[str, Any]:
    pet = get_cat_profile(user_id, conversation["pet_id"]) if conversation.get("pet_id") else None
    context: dict[str, Any] = {
        "pet_profile": pet, "current_food": None, "food_list": [], "recent_daily_records": [],
        "food_candidates": [], "food_analysis": None, "food_submission": None,
        "diet_history": [], "diet_history_status": "unavailable",
    }
    packet = (state.get("slots") or {}).get(GUARDIAN_CONTEXT_SLOT)
    if isinstance(packet, dict) and packet.get("pet_id") == conversation.get("pet_id"):
        context["guardian_context"] = deepcopy(packet)
    supplied_context = client_context if isinstance(client_context, dict) else {}
    context["recent_daily_records"] = _normalize_daily_records(supplied_context.get("recent_daily_records"))
    if pet and (pet.get("food_brand") or pet.get("food_product")):
        signature = [pet.get(key) for key in ("food_brand", "food_product", "food_formula_id")]
        previous_signature = state["slots"].get("_profile_diet_signature")
        if previous_signature is not None and previous_signature != signature:
            state["slots"].pop("current_food", None)
            state['slots'].pop('food_tolerance', None)
            state['slots'].pop('_tolerated_food', None)
            pipeline = state["slots"].get(SOFT_STOOL_STATE_SLOT)
            if isinstance(pipeline, dict):
                pipeline.setdefault("context", {}).update(baseline_loaded=False, followup_context_loaded=False)
                pipeline.setdefault("slots", {}).pop("current_product", None)
                pipeline.setdefault("slot_status", {}).pop("current_product", None)
        state["slots"]["_profile_diet_signature"] = signature
        context["current_food"] = {
            "brand": pet.get("food_brand"), "product_name": pet.get("food_product"),
            "product_id": pet.get("food_product_id"), "formula_id": pet.get("food_formula_id"),
            "catalog_key": f"formula:{pet['food_formula_id']}" if pet.get("food_formula_id") else None,
        }
        state["slots"]["profile_current_food"] = " ".join(filter(None,[pet.get("food_brand"),pet.get("food_product")]))
        if not state["slots"].get("current_food"):
            state["slots"]["current_food"] = state["slots"]["profile_current_food"]
    if pet and conversation.get("pet_id"):
        try:
            history = list_diet_history(user_id, conversation["pet_id"], limit=10)
            context["diet_history"] = compact_diet_context(history["items"])
            context["diet_history_status"] = "available" if history["items"] else "no_records"
            context["diet_association_note"] = history["association_note"]
        except (pymysql.MySQLError, LookupError):
            pass
    try:
        foods = list_food_submissions(user_id, limit=20).get("items") or []
        context["food_list"] = [
            {
                "brand": item.get("recognized_brand") or item.get("claimed_brand") or "",
                "product_name": item.get("recognized_product_name") or item.get("claimed_product_name") or "",
                "recognition_status": item.get("recognition_status"),
                "catalog_key": item.get("catalog_key") or "",
            }
            for item in foods if item.get("status") != "cancelled"
        ][:20]
    except Exception:
        context["food_list"] = []
    if state.get('primary_intent') == 'food_switch' and pet and conversation.get('pet_id'):
        try:
            context['diet_reference_history'] = list_diet_references(user_id, conversation['pet_id'])
        except pymysql.MySQLError:
            context['diet_reference_history'] = context['diet_history']
            context['history_exclusion_status'] = 'unavailable'
    slots = state.get("slots") or {}
    food_name = _clean(slots.get("food_name") or slots.get("target_food"))
    if food_name:
        brand = _clean(slots.get("food_brand") or (None if slots.get("target_food") else (pet or {}).get("food_brand")))
        if brand:
            context["food_candidates"] = list_standard_product_candidates(brand, product=food_name, limit=10)
    catalog_key = _clean(slots.get("catalog_key"),128)
    if catalog_key:
        try:
            context["food_analysis"] = get_catalog_product_ingredients({"catalog_key":catalog_key})
            formula_id = (context['food_analysis'].get('product') or {}).get('formula_id')
            current_id = (pet or {}).get('food_formula_id')
            portraits = load_product_portraits([formula_id, current_id])
            context['food_analysis']['product_portrait'] = portraits.get(formula_id)
            context['food_analysis']['portrait_comparison'] = compare_portraits(portraits.get(current_id), portraits.get(formula_id))
        except Exception:
            pass
    for attachment in attachments:
        if attachment.get("type") == "food_submission" and attachment.get("submission_id"):
            context["food_submission"] = get_food_submission(user_id, attachment["submission_id"])["item"]
            state["slots"]["ingredient_source"] = "food_submission"
            break
    return context


def _evaluate_flow(state: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    intent, slots = state.get("primary_intent") or "general_pet_qa", state.get("slots") or {}
    warning = slots.get("warning_signs") or []
    dangerous = {"blood_in_stool", "poor_mental_status"}
    reasons = [item for item in warning if item in dangerous] if isinstance(warning,list) else []
    if slots.get("severity") == "severe": reasons.append("severe_symptom")
    if intent == "symptom_consult" and reasons:
        return {"status":"risk_interrupt","missing_slots":[],"risk":{"level":"high","reasons":reasons}}
    config = FLOW_CONFIG[intent]
    missing = []
    for slot in config["required"]:
        value = slots.get(slot)
        filled = value is not None and value != "" and value != []
        if slot == "current_food": filled = filled or bool(context.get("current_food"))
        if not filled: missing.append(slot)
    if not missing: return {"status":"ready_to_answer","missing_slots":[],"risk":{"level":"low","reasons":[]}}
    if state.get("followup_count",0) >= config["max_followups"]:
        return {"status":"answer_with_limited_info","missing_slots":missing,"risk":{"level":"low","reasons":[]}}
    next_slot = next((slot for slot in config["priority"] if slot in missing), missing[0])
    return {"status":"need_more_info","next_slot":next_slot,"missing_slots":missing,"question":SLOT_QUESTIONS.get(next_slot)}


def _three_point_text(data: dict[str, Any]) -> str:
    points = []
    for key in ("context", "care", "watch"):
        value = _clean(data.get(key), 90)
        if not value:
            raise ValueError(f"模型结果缺少 {key}")
        value = value.replace("初步判断", "目前看").replace("判断", "看起来")
        value = value.replace("我的建议是", "现在可以").replace("建议", "可以考虑")
        points.append(value)
    return "\n".join(f"{index}. {value}" for index, value in enumerate(points, 1))


def _generate_answer(message: str, state: dict[str, Any], context: dict[str, Any], limited: bool) -> tuple[str, str | None]:
    cfg = get_chat_model_config()
    fallback = (
        "1. 从目前的信息看，常见的饮食变化、环境变化或短暂不适都可能带来这种表现。\n"
        "2. 这两天可以先让饮食和作息保持稳定，保证饮水，并记录精神、食欲和排便变化。\n"
        "3. 如果一直没有缓解，或出现便血、频繁呕吐、精神明显变差，请尽快联系宠物医院。"
    )
    if state.get("primary_intent") == "food_switch":
        fallback = (
            "1. 选粮会结合当前饮食、以前吃粮的表现和已有健康背景，配方评分只作为参考。\n"
            "2. 先对照以前吃得适应的粮，比较候选粮的蛋白来源、脂肪和纤维资料，避免同时新增多种食物。\n"
            "3. 可以告诉我你考虑的品牌或具体粮款、预算与偏好，我再帮你对比；已有疾病需结合医生的饮食要求。"
        )
        if context.get("recommendation"):
            fallback += "\n" + context["recommendation"]["note"]
    if not cfg["api_key"]:
        return fallback, None
    payload = {
        "latest_message": message,
        "intent": state.get("primary_intent"),
        "slots": {key: value for key, value in (state.get('slots') or {}).items() if not key.startswith('_')},
        "context": context,
        "limited_information": limited,
        "output": {"context": "少量原因或背景", "care": "现在可以怎么照顾", "watch": "接下来留意什么"},
    }
    try:
        response = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60).chat.completions.create(
            model=cfg["model"], temperature=0.2, response_format={"type":"json_object"}, messages=[
                {"role":"system","content":(
                    "你是宠物饮食与日常照护管家，围绕饮食分析、个体吃粮表现和行动建议提供持续服务。"
                    "只依据提供的数据回答，不得编造产品、配料、评分或疾病结论。"
                    "回答前综合pet_profile、current_food、food_list和recent_daily_records；明确区分没有记录与数值为零，"
                    "guardian_context含前一个场景的事实与来源，切换选粮时沿用这些信息，不重复开展症状问诊。"
                    "diet_history是已确认的宠物吃粮记录；food_list仅为用户上传的产品，不代表吃过。"
                    "food_analysis_scope为current时直接分析档案当前粮，不得再追问粮名；为history时逐条使用food_history_analyses；"
                    "为current_vs_history时必须把food_analysis中的当前粮与food_history_analyses中的历史粮逐项对比，明确说出产品名、共同点、差异和取舍，不得漏掉任一侧。"
                    "food_history_analyses中analysis_status=unavailable只表示该条缺少可核验的配方分析，不得忽略该条已确认吃粮记录。"
                    "health_topics只表示同期咨询话题，不能推断疾病或粮食导致症状。日期未知不推断，资料不可用不当作没有。"
                    "只有food_candidates中已有且有适用依据的产品才能作为具体候选；没有候选就给筛选方向，不编造品牌推荐。"
                    "宠物年龄是可选信息，未填写年龄也可展示已有候选，不要求先补年龄才能推荐；不要猜测年龄或声称年龄适配已验证，沿用候选中的适用阶段说明。"
                    "recommendation.scenario是已执行的业务场景规则，保持项与改善项不可自行更改；症状阶段与长期目标不同，不能把恢复期当健康无症状。"
                    "产品画像仅解释结构特点，互补不代表混喂或治疗；缺画像、能量或适口性数据时不得宣称已核验对应效果。"
                    "有饮水、进食、排便次数或便便形态记录时应优先结合其近期变化，但不要虚构正常参考值。"
                    "只输出合法JSON，且必须正好包含context、care、watch三个字符串字段。"
                    "context用一两句话解释少量原理或背景，care给出最重要且可执行的做法，watch说明观察重点和需要联系宠物医院的情况。"
                    "每个字段40到70个汉字，总体简洁；不要使用“判断”“建议”“诊断结果”等机械措辞，不要添加标题、编号或JSON以外内容。"
                    "可以自然使用一处“呀、呢、哦”，但不要称呼用户为亲、宝子或主人。"
                )},
                {"role":"user","content":_json_dumps(payload)},
            ], **_provider_options(cfg))
        parsed = _parse_model_json(response.choices[0].message.content or "")
        return _three_point_text(parsed), cfg["model"]
    except Exception:
        return fallback, None


def _handle_context_message(user_id, conversation, message, state, source_message_id=None,
                            interaction=None, attachments=None, client_context=None,
                            forced_intent=None, intent_result=None):
    attachments = attachments or []
    client_context = client_context or {}
    model_name = None
    previous_food_goal = state.get('slots', {}).get('food_goal')
    previous_scene = (state['slots'].get('food_health_state'), state['slots'].get('food_health_stage'))
    if intent_result and intent_result[0].get("dialogue_action") == "pause":
        _pause_soft_stool(state)
        state.update(primary_intent="general_pet_qa", secondary_intent=None, current_step=None,
                     missing_slots=[], followup_count=0)
        state["turn_count"] += 1
        return {"response_type": "text", "reply": "好的，先把这个话题放一放。接下来你想聊什么？",
                "result": {"intent": "general_pet_qa", "turn_no": state["turn_count"], "sequence_no": 1}}
    handoff = forced_intent == "food_switch" and not interaction and not attachments
    if handoff:
        _prepare_food_handoff(state, conversation.get("pet_id"),
                              (intent_result[0].get("_soft_extraction") if intent_result else None), source_message_id)
    elif state.get("slots", {}).get(SOFT_STOOL_STATE_SLOT):
        _share_guardian_context(state, conversation.get("pet_id"),
                               (intent_result[0].get("primary_intent") if intent_result else None) or "general",
                               (intent_result[0].get("_soft_extraction") if intent_result else None), source_message_id)
        _pause_soft_stool(state)
    if interaction:
        slot = _clean(interaction.get("slot"),64)
        if not slot: raise ValueError("interaction.slot 不能为空")
        scene_options = {'food_health_state': STATES, 'food_health_stage': STAGES,
                         'food_tolerance': {'current_good': '当前粮吃得稳定', 'unknown': '暂不清楚'}}
        if slot in scene_options and interaction.get('value') not in scene_options[slot]:
            raise ValueError('请选择有效的身体状态、阶段或耐受情况')
        if slot.startswith('_'):
            raise ValueError('不能修改内部会话信息')
        if slot == 'food_health_state':
            state['slots'].pop('food_health_stage', None)
        state["slots"][slot] = interaction.get("value")
        state["turn_count"] += 1
    elif attachments and not message:
        state["primary_intent"] = "ingredient_analysis"
        state["turn_count"] += 1
    else:
        pending_step = state.get("current_step")
        extraction, model_name = intent_result or _extract_intent(message, state)
        if forced_intent:
            extraction = {**extraction, "primary_intent": forced_intent, "secondary_intent": None,
                          "slots": {key: value for key, value in (extraction.get("slots") or {}).items() if key in INTENT_SLOTS[forced_intent]}}
        state["primary_intent"] = extraction["primary_intent"]
        state["secondary_intent"] = extraction.get("secondary_intent")
        state["slots"].update(extraction.get("slots") or {})
        if pending_step == "warning_signs" and message and not state["slots"].get("warning_signs"):
            state["slots"]["warning_signs"] = _warning_signs_from_text(message)
        if pending_step == "symptom_context" and message:
            state["slots"]["symptom_context"] = message
        state["turn_count"] += 1
    if attachments and not state.get("primary_intent"):
        state["primary_intent"] = "ingredient_analysis"
    context = _build_context(user_id, conversation, state, attachments, client_context)
    _prepare_saved_food_analysis(message, state, context)
    if state.get("primary_intent") == "food_switch":
        state["slots"].update(explicit_preferences(message))
        pending = state.get('current_step')
        state['slots'].update(extract_health(message, state['slots'], pending))
        if state['slots'].get('food_health_state') == 'none':
            state['slots']['food_health_stage'] = 'none'
        if pending == 'food_tolerance' and message:
            if re.search(r'不清楚|不知道|不确定|不耐受|不适应', message):
                state['slots']['food_tolerance'] = 'unknown'
            elif re.search(r'当前粮|现在这款|这款粮', message) and re.search(r'耐受良好|吃得稳定|吃得适应|适应良好', message):
                state['slots']['food_tolerance'] = 'current_good'
        if ((interaction or {}).get('slot') == 'food_tolerance' or pending == 'food_tolerance') and state['slots'].get('food_tolerance') == 'current_good':
            state['slots']['_tolerated_food'] = state['slots'].get('current_food')
        if previous_scene != (state['slots'].get('food_health_state'), state['slots'].get('food_health_stage')):
            # A previous selection/plan is no longer a decision for the new state.
            # Keep factual feeding records, but never silently reuse an old plan.
            previous_flow = state['slots'].get(FOOD_FLOW_STATE_KEY) or {}
            if previous_flow.get('stage') not in {'started', 'completed'}:
                state['slots'].pop(FOOD_FLOW_STATE_KEY, None)
                state['slots'].pop('target_food', None)
            else:
                previous_flow.pop('candidates', None)
        previous_goal = previous_food_goal
        choices = goals_in_text(message)
        # A symptom is a state, not a competing long-term goal.
        if state['slots'].get('food_health_state') not in (None, 'none', 'unknown'):
            long_term = [goal for goal in choices if goal not in {'soft_stool', 'black_chin'}]
            choices = long_term or choices
            if previous_goal and not long_term and extract_health(message, state['slots']) and not re.search(r'希望|想改善|目标|诉求', message):
                choices = []
        historical_feedback = bool(re.search(r'(以前|之前|过去|曾经).{0,8}(吃|喂|用)', message))
        if historical_feedback or (pending == 'food_tolerance' and message):
            state['slots']['_food_history_feedback'] = message
            history_feedbacks = state['slots'].setdefault('_food_history_feedbacks', [])
            if message not in history_feedbacks:
                history_feedbacks.append(message)
        if interaction and interaction.get('slot') == 'food_goal':
            value = interaction.get('value')
            if value not in GOALS:
                raise ValueError('请选择一个有效的选粮诉求')
            state['slots']['food_goal'] = value
        elif len(choices) == 1 and not historical_feedback:
            state['slots']['food_goal'] = choices[0]
        elif len(choices) > 1 and not historical_feedback and not state['slots'].get('food_goal'):
            state['slots']['food_goals'] = choices
        elif pending == 'food_goal' and re.search(r'不知道|随便|没有特别|无所谓', message):
            state['slots']['food_goal'] = 'general'
        if previous_goal and state['slots'].get('food_goal') != previous_goal and (state['slots'].get(FOOD_FLOW_STATE_KEY) or {}).get('target'):
            state['slots'].pop(FOOD_FLOW_STATE_KEY, None)
            state['slots'].pop('target_food', None)
        if pending == 'current_food' and message:
            if re.search(r'不清楚|不知道|暂时没有|还没吃', message):
                state['slots']['_current_food_unknown'] = True
            else:
                state['slots']['current_food'] = message
        state['slots']['_food_scenario'] = build_scenario(state['slots'])
    submission = context.get("food_submission")
    if submission and submission.get("recognition_status") in {"pending","processing"}:
        reply = {"response_type":"processing","reply":submission.get("recognition_message") or "配料表正在识别，请稍后刷新投稿后再交给我分析。",
                 "result":{"resource":"food_submission","id":submission["id"],"interval_ms":2000}}
    elif submission and (submission.get("status") == "cancelled" or
                         submission.get("recognition_status") != "success" or
                         not _clean(submission.get("ingredient_composition"))):
        reply = {"response_type": "text", "reply": submission.get("recognition_message") or
                 "还没有可分析的配料表。请拍包装背面的配料表近照，让文字完整、清晰、不反光，再重新上传。",
                 "result": {"resource": "food_submission", "id": submission["id"],
                            "recognition_status": submission.get("recognition_status"),
                            "can_reupload": submission.get("can_reupload", False)}}
    else:
        food_reply = handle_food_flow(user_id, conversation, message, state, context, interaction) if state.get("primary_intent") == "food_switch" else None
        if not food_reply and state.get('primary_intent') == 'food_switch':
            goal = state['slots'].get('food_goal') or recommendation_goal(state['slots'])
            scene_question = scenario_question(state['slots']['_food_scenario'])
            if scene_question:
                field, text, choices = scene_question
                flow = {'status': 'need_more_info', 'next_slot': field, 'question': {
                    'text': text, 'response_type': 'single_select',
                    'options': [{'label': label, 'value': key} for key, label in choices.items()]}}
            elif state['slots']['_food_scenario']['status'] in {'limited', 'blocked'}:
                flow = {'status': 'ready_to_answer'}
            elif not goal:
                flow = {'status': 'need_more_info', 'next_slot': 'food_goal', 'question': {'text': '这次最希望改善哪一方面？有多个关注点时，先选最优先的；其余信息我会保留。', 'response_type': 'single_select', 'options': goal_options()}}
            elif state['slots'].get('_food_mode') == 'switch' and not state['slots'].get('current_food') and not state['slots'].get('_current_food_unknown'):
                flow = {'status': 'need_more_info', 'next_slot': 'current_food', 'question': {'text': '它现在主要吃哪款粮？告诉我品牌和粮名就好；暂时不知道也可以继续。', 'response_type': 'text_or_select', 'options': []}}
            else:
                state['slots']['food_goal'] = goal
                flow = {'status': 'ready_to_answer'}
        else:
            flow = {'status': 'food_flow'} if food_reply else _evaluate_flow(state, context)
        state["missing_slots"] = flow.get("missing_slots") or []
        if food_reply:
            reply = food_reply
        elif flow["status"] == "need_more_info":
            state["current_step"] = flow["next_slot"]
            state["followup_count"] += 1
            question = flow.get("question") or {}
            response_type = question.get("response_type", "text")
            options = question.get("options", [])
            reply = {
                "response_type": response_type,
                "reply": question.get("text", "还需要补充一些信息。"),
                "interaction": (
                    {"slot": flow["next_slot"], "options": options}
                    if response_type != "text" or options else None
                ),
            }
        elif flow["status"] == "risk_interrupt":
            state["current_step"], state["risk_level"] = None, "high"
            reply = {"response_type":"risk_alert","reply":(
                        "1. 现在已经出现需要优先留意的风险信号，先不要继续观察等待。\n"
                        "2. 请尽快联系附近的宠物医院，并按医生安排就诊。\n"
                        "3. 出发前记录症状开始时间、饮食变化和排泄情况，途中注意保暖与安静。"
                    ),
                     "result":{"risk":flow["risk"],"disclaimer":"AI内容仅供参考，不能替代兽医诊断"}}
        else:
            state["current_step"], state["risk_level"] = None, "low"
            if state.get("primary_intent") == "food_switch":
                packet = context.get('guardian_context') or {}
                state['slots']['_confirmed_history_text'] = (packet.get('facts') or {}).get('history_foods_reviewed') or ''
                recommendation = recommend_guardian_foods(context.get("pet_profile") or {}, state["slots"], context.get('diet_reference_history', context['diet_history']))
                if context.get('history_exclusion_status') == 'unavailable':
                    recommendation = {'status': 'data_unavailable', 'items': [], 'note': '历史口粮暂时无法读取，为避免重复推荐已吃过的粮，请稍后再试。'}
                context["recommendation"] = recommendation
                context["food_candidates"] = recommendation["items"]
                food_state = state["slots"].setdefault(FOOD_FLOW_STATE_KEY, {"stage": "selection"})
                food_state["candidates"] = recommendation["items"]
                food_state['reference_food_text'] = state['slots'].get('current_food')
            recommendation = context.get('recommendation') or {}
            if state.get('primary_intent') == 'food_switch' and not context.get('food_candidates'):
                text, answer_model = recommendation.get('note') or '暂未找到可核验的候选。', None
            elif comparison_text := _saved_food_comparison_text(context):
                text, answer_model = comparison_text, None
            else:
                text, answer_model = _generate_answer(message,state,context,flow["status"]=="answer_with_limited_info")
            if state.get("primary_intent") == "food_switch" and context.get("food_candidates"):
                text += '\n' + recommendation_text(context['recommendation'])
                text += "\n可以回复“选第一款”，先看看营养、风险和适配理由；确定要换到这款时，再继续换粮安排。选中时不会更新口粮档案。"
            model_name = answer_model or model_name
            reply = {"response_type":"result_card","reply":text,
                     "result":{"intent":state.get("primary_intent"),"slots":state.get("slots"),
                               "limited_info":flow["status"]=="answer_with_limited_info",
                               "food_candidates":context.get("food_candidates") or [],
                               "recommendation":context.get("recommendation"),
                               "actions": [{"type": "diet_history", "label": "查看历史口粮", "pet_id": conversation.get("pet_id")}] if conversation.get("pet_id") else [],
                               "food_submission":submission},
                     "disclaimer":"AI内容仅供参考，不能替代兽医诊断"}
            if recommendation.get('status') == 'baseline_needed' and not state['slots'].get('food_tolerance'):
                state['current_step'] = 'food_tolerance'
                reply.update(response_type='text_or_select', reply=text + '\n当前粮是否曾吃得稳定？也可以告诉我哪款历史粮吃得适应。',
                    interaction={'slot': 'food_tolerance', 'options': [
                        {'label': '当前粮曾吃得稳定', 'value': 'current_good'}, {'label': '暂不清楚', 'value': 'unknown'}]})
    result = {'intent': state.get('primary_intent'), **(reply.get("result") or {}), "turn_no": state["turn_count"],
              "sequence_no": 1, "model_name": model_name}
    return {**reply, "result": result, "messages": [{**reply, "result": result}]}


def _handle_food_switch_message(*args, **kwargs):
    state, message = args[3], args[2]
    decision = (kwargs.get('intent_result') or ({}, None))[0]
    wants_switch = re.search(r'换粮|换主粮|换猫粮|换狗粮|换成|换到', message) and not re.search(r'(?:不|别|不用|不要|暂时不).{0,3}换', message)
    state['slots']['_food_mode'] = 'switch' if wants_switch else decision.get('_food_mode') or state['slots'].get('_food_mode') or 'switch'
    return _handle_context_message(*args, **kwargs, forced_intent="food_switch")


def _handle_food_selection_message(*args, **kwargs):
    args[3]['slots']['_food_mode'] = 'selection'
    return _handle_context_message(*args, **kwargs, forced_intent='food_switch')


def _handle_reminder_message(user_id, conversation, message, state, source_message_id=None,
                            interaction=None, attachments=None, client_context=None, intent_result=None):
    _share_guardian_context(state, conversation.get('pet_id'), 'reminder')
    _pause_soft_stool(state)
    capabilities = (client_context or {}).get('capabilities') or {}
    if not isinstance(capabilities, dict) or capabilities.get('cloud_reminders_v1') is not True:
        state.update(primary_intent='general_pet_qa', current_step=None)
        state['turn_count'] += 1
        return {'response_type': 'text', 'reply': '当前页面还不能直接保存这条提醒。请到“宠物 → 我的提醒”设置；这次对话没有新增提醒。',
                'result': {'intent': 'reminder', 'status': 'client_update_required', 'turn_no': state['turn_count']}}
    pet = get_cat_profile(user_id, conversation['pet_id']) if conversation.get('pet_id') else None
    reply = handle_reminder(user_id, conversation, message, state, pet, interaction=interaction)
    result = {**reply.get('result', {}), 'turn_no': state['turn_count'], 'sequence_no': 1}
    return {**reply, 'result': result, 'messages': [{**reply, 'result': result}]}


def _recognize_guardian_turn(message, state):
    """Reuse slot extraction for semantic handoffs; never add a routing LLM call
    to an existing soft-stool turn. Clear requests/short answers stay offline.
    """
    nested = (state.get("slots") or {}).get(SOFT_STOOL_STATE_SLOT)
    from soft_stool_assistant.llm import _extract_rule_info, extract_user_info
    from soft_stool_assistant.policy import urgent_risk
    literal = _extract_rule_info(message, nested or {})
    health_update = extract_health(message, state.get('slots') or {})
    if health_update.get('food_health_state') == 'vomiting' and health_update.get('food_health_stage') in {'recovering', 'recovered'}:
        # An explicit recovery report must not become a new vomiting episode.
        literal.pop('vomiting', None)
    if urgent_risk(literal):
        return {"primary_intent": "symptom_consult", "slots": {}, "_target_flow": "soft_stool",
                "_soft_extraction": {**literal, "_field_evidence": {key: message for key in literal}}}, None
    target = requested_flow(message)
    if target == 'reminder' or (state.get('primary_intent') == 'reminder' and state.get('current_step') and not target and not SOFT_STOOL_PAUSE_RE.search(message)):
        return {'primary_intent': 'reminder', 'slots': {}, '_target_flow': 'reminder'}, None
    if target:
        return {"primary_intent": target, "slots": {}, "_target_flow": target,
                '_food_mode': 'switch' if re.search(r'换粮|换一|换款|换猫粮|换狗粮|换主粮', message) else 'selection',
                "_soft_extraction": {**literal, "_field_evidence": {key: message for key in literal}} if nested else None}, None
    if state.get('primary_intent') == 'food_switch' and state.get('current_step') in {
            'food_health_state', 'food_health_stage', 'food_tolerance'} and not SOFT_STOOL_OTHER_TOPIC_RE.search(message) and not SOFT_STOOL_PAUSE_RE.search(message):
        return {'primary_intent': 'food_switch', 'slots': {}, '_target_flow': 'food_switch'}, None
    if state.get('primary_intent') == 'food_switch' and extract_health(message, state.get('slots') or {}) and not SOFT_STOOL_OTHER_TOPIC_RE.search(message) and not SOFT_STOOL_PAUSE_RE.search(message):
        return {'primary_intent': 'food_switch', 'slots': {}, '_target_flow': 'food_switch'}, None
    if state.get('primary_intent') == 'food_switch' and re.search(r'(以前|之前|过去|曾经).{0,8}(吃|喂|用)', message):
        return {'primary_intent': 'food_switch', 'slots': {}, '_target_flow': 'food_switch'}, None
    food_progress = state.get("current_step") or ((state.get("slots") or {}).get(FOOD_FLOW_STATE_KEY) or {}).get("stage") == "choose_target" or re.search(r"选|换|目标|开始|吃|喂|食欲|精神|便便|排便|呕吐|预算|每斤|国产|进口|肠胃|皮肤|泪痕|体重|过敏|推荐|还没|暂时|继续|方案|比较|对比", message)
    food_preference = (state.get('current_step') in {'food_goal', 'target_requirement'} or re.search(r'希望|想改善|偏好|友好', message)) and not re.search(r'今天|刚刚|突然|最近|出现|开始拉|又拉', message)
    if state.get("primary_intent") == "food_switch" and (food_progress or food_preference) and (not SOFT_STOOL_TRIGGER_RE.search(message) or food_preference) and not SOFT_STOOL_PAUSE_RE.search(message) and not SOFT_STOOL_OTHER_TOPIC_RE.search(message):
        return {"primary_intent": "food_switch", "slots": {'target_requirement': message} if food_preference else {}, "_target_flow": "food_switch"}, None
    if SOFT_STOOL_OTHER_TOPIC_RE.search(message):
        extraction = _fallback_extraction(SOFT_STOOL_PAUSE_RE.sub("", message), {"primary_intent": None})
        return {**extraction, "_target_flow": "general"}, None
    if SOFT_STOOL_PAUSE_RE.search(message):
        return {"primary_intent": "general_pet_qa", "slots": {}, "_target_flow": "general", "dialogue_action": "pause"}, None
    if (state.get("primary_intent") == "food_switch" and state.get("current_step") == "target_requirement"
            and not re.search(r"(?:继续|恢复|再聊).{0,5}软便", message)):
        # '希望改善软便' answers the food-selection question; it must not
        # restart a consultation just because it contains a symptom word.
        return {"primary_intent": "food_switch", "slots": {"target_requirement": message}, "_target_flow": "food_switch"}, None
    if _should_route_soft_stool(message, state):
        routing_state = deepcopy(nested) if nested else {"slots": {}, "context": {}}
        routing_state.setdefault("context", {})["available_flows"] = [strategy.name for strategy in GUARDIAN_STRATEGIES]
        extracted = extract_user_info(message, routing_state)
        dialogue = extracted.get("_dialogue") or {}
        target = dialogue.get("target_flow", "soft_stool")
        intent = "food_switch" if target == "food_switch" else "symptom_consult" if target == "soft_stool" else dialogue.get("target_intent", "general_pet_qa")
        return {"primary_intent": intent, "slots": {}, "_target_flow": target,
                "dialogue_action": dialogue.get("action", "continue"), "_soft_extraction": extracted}, None
    intent_state = deepcopy(state)
    if state.get('primary_intent') == 'reminder' and not state.get('current_step'):
        intent_state.update(primary_intent=None, current_step=None)
    if nested and nested.get("status") == "answered":
        intent_state.update(primary_intent=None, current_step=None)
    extraction, model = _extract_intent(message, intent_state)
    return {**extraction, "_target_flow": "food_switch" if extraction["primary_intent"] == "food_switch" else "general"}, model


GENERAL_STRATEGY = FlowStrategy("general", lambda message, state: True, _handle_context_message)
GUARDIAN_STRATEGIES = (
    FlowStrategy("soft_stool", lambda message, state: state.get("_turn_decision", {}).get("_target_flow") == "soft_stool", _handle_soft_stool_message),
    FlowStrategy('reminder', lambda message, state: state.get('_turn_decision', {}).get('_target_flow') == 'reminder', _handle_reminder_message),
    FlowStrategy('food_selection', lambda message, state: state.get('_turn_decision', {}).get('_target_flow') == 'food_switch' and state.get('_turn_decision', {}).get('_food_mode') == 'selection', _handle_food_selection_message),
    FlowStrategy("food_switch", lambda message, state: state.get("_turn_decision", {}).get("_target_flow") == "food_switch", _handle_food_switch_message),
    GENERAL_STRATEGY,
)


def handle_message(user_id: Any, conversation_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    user_id, conversation_id = _clean(user_id,128), _clean(conversation_id,32)
    conversation = _get_conversation(user_id, conversation_id)
    message = _clean(payload.get("message"), MAX_MESSAGE_LENGTH)
    interaction = payload.get("interaction") if isinstance(payload.get("interaction"),dict) else None
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"),list) else []
    client_context = payload.get("client_context") if isinstance(payload.get("client_context"),dict) else {}
    if not message and not interaction and not attachments:
        raise ValueError("message、interaction、attachments 至少提供一项")
    state = _load_state(conversation_id)
    user_message = _save_message(conversation_id=conversation_id,user_id=user_id,role="user",content=message,
                  interaction=interaction,attachments=attachments,
                  result={"turn_no": state["turn_count"] + 1, "sequence_no": 0})
    intent_result = _recognize_guardian_turn(message, state) if message and not interaction and not attachments else None
    routing_state = {**state, "_turn_decision": intent_result[0] if intent_result else ({'_target_flow': 'reminder'} if interaction and state.get('primary_intent') == 'reminder' else {})}
    strategy = select_strategy(message, routing_state, GUARDIAN_STRATEGIES)
    if strategy.name == "soft_stool" and not conversation.get("pet_id"):
        strategy = GENERAL_STRATEGY
    reply = strategy.handle(user_id, conversation, message, state, user_message["id"],
                            interaction=interaction, attachments=attachments,
                            client_context=client_context, intent_result=intent_result)
    _save_state(state)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE {CONVERSATION_TABLE} SET primary_intent=%s,updated_at=%s WHERE id=%s AND user_id=%s",
                           (state.get("primary_intent"), _now(), conversation_id, user_id))
        conn.commit()
    saved_messages = []
    for item in reply.get("messages") or [reply]:
        saved = _save_message(conversation_id=conversation_id, user_id=user_id, role="assistant",
                             content=item["reply"], response_type=item.get("response_type"),
                             interaction=item.get("interaction"), result=item.get("result") or reply.get("result"),
                             model_name=(item.get("result") or {}).get("model_name"))
        saved["disclaimer"] = reply.get("disclaimer")
        saved_messages.append(saved)
    return {"ok": True, "message": saved_messages[-1], "messages": saved_messages}
