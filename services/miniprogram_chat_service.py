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
from services.miniprogram_food_submission_service import get_food_submission


CONVERSATION_TABLE = "miniprogram_chat_conversation"
STATE_TABLE = "miniprogram_chat_state"
MESSAGE_TABLE = "miniprogram_chat_message"
PROMPT_VERSION = "pet-manager-chat-v1"
MAX_MESSAGE_LENGTH = 4000

VALID_INTENTS = {
    "symptom_consult", "food_switch", "food_analysis",
    "ingredient_analysis", "general_pet_qa",
}

INTENT_SLOTS = {
    "symptom_consult": {
        "symptom", "duration_days", "frequency_per_day", "severity", "vomiting",
        "blood_in_stool", "mental_status", "appetite_status", "warning_signs",
        "recent_food_change", "symptom_context",
    },
    "food_switch": {"current_food", "switch_reason", "target_food", "target_requirement", "recent_symptom"},
    "food_analysis": {"food_name", "catalog_key"},
    "ingredient_analysis": {"ingredient_name", "food_name", "ingredient_source"},
    "general_pet_qa": {"topic"},
}

FLOW_CONFIG = {
    "symptom_consult": {
        "required": ["symptom", "warning_signs", "symptom_context"],
        "priority": ["warning_signs", "symptom_context"],
        "max_followups": 2,
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
        "text": "听起来它现在有点不舒服，我先陪你一起理一理。除了刚才说的情况，还有没有呕吐、便血、精神明显变差或不太想吃东西呢？", "response_type": "multi_select",
        "options": [
            {"label": "没有", "value": "none"}, {"label": "呕吐", "value": "vomiting"},
            {"label": "便血", "value": "blood_in_stool"},
            {"label": "精神明显变差", "value": "poor_mental_status"},
            {"label": "食欲明显下降", "value": "poor_appetite"},
        ],
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
    "symptom_context": {"text": "好的，我们再确认最后一点：这种情况大概持续多久了？最近有没有刚换粮、加罐头或者吃新的零食呀？", "response_type": "text", "options": []},
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
            cursor.execute(f"SELECT * FROM {MESSAGE_TABLE} WHERE conversation_id=%s AND user_id=%s AND status='active' ORDER BY created_at ASC LIMIT %s", (conversation_id,user_id,limit))
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
        return {"primary_intent": "symptom_consult", "secondary_intent": "food_switch" if "换粮" in message else None, "slots": {"symptom": symptom}, "confidence": 0.7}
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
              "current_step": state.get("current_step"), "current_slots": state.get("slots"), "latest_message": message,
              "output": {"primary_intent": "", "secondary_intent": None, "slots": {}, "confidence": 0.0}}
    try:
        response = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=45).chat.completions.create(
            model=cfg["model"], temperature=0, response_format={"type":"json_object"}, messages=[
                {"role":"system","content":"你是宠物咨询的意图与信息抽取模块。只提取用户明确表达的信息，不回答问题，只输出JSON。"},
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


def _build_context(user_id: str, conversation: dict[str, Any], state: dict[str, Any], attachments: list[dict[str, Any]]) -> dict[str, Any]:
    pet = get_cat_profile(user_id, conversation["pet_id"]) if conversation.get("pet_id") else None
    context: dict[str, Any] = {"pet_profile": pet, "current_food": None, "food_candidates": [], "food_analysis": None, "food_submission": None}
    if pet and (pet.get("food_brand") or pet.get("food_product")):
        context["current_food"] = {"brand":pet.get("food_brand"),"product_name":pet.get("food_product")}
        if not state["slots"].get("current_food"):
            state["slots"]["current_food"] = " ".join(filter(None,[pet.get("food_brand"),pet.get("food_product")]))
    slots = state.get("slots") or {}
    food_name = _clean(slots.get("food_name") or slots.get("target_food"))
    if food_name:
        brand = _clean(slots.get("food_brand") or (pet or {}).get("food_brand"))
        if brand:
            context["food_candidates"] = list_standard_product_candidates(brand, product=food_name, limit=10)
    catalog_key = _clean(slots.get("catalog_key"),128)
    if catalog_key:
        try:
            context["food_analysis"] = get_catalog_product_ingredients({"catalog_key":catalog_key})
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
    if not cfg["api_key"]:
        return fallback, None
    payload = {
        "latest_message": message,
        "intent": state.get("primary_intent"),
        "slots": state.get("slots"),
        "context": context,
        "limited_information": limited,
        "output": {"context": "少量原因或背景", "care": "现在可以怎么照顾", "watch": "接下来留意什么"},
    }
    try:
        response = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60).chat.completions.create(
            model=cfg["model"], temperature=0.2, response_format={"type":"json_object"}, messages=[
                {"role":"system","content":(
                    "你是温柔、耐心、专业的女性宠物护理助手，语气像一位亲切的护士。"
                    "只依据提供的数据回答，不得编造产品、配料、评分或疾病结论。"
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


def handle_message(user_id: Any, conversation_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    user_id, conversation_id = _clean(user_id,128), _clean(conversation_id,32)
    conversation = _get_conversation(user_id, conversation_id)
    message = _clean(payload.get("message"), MAX_MESSAGE_LENGTH)
    interaction = payload.get("interaction") if isinstance(payload.get("interaction"),dict) else None
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"),list) else []
    if not message and not interaction and not attachments:
        raise ValueError("message、interaction、attachments 至少提供一项")
    _save_message(conversation_id=conversation_id,user_id=user_id,role="user",content=message,
                  interaction=interaction,attachments=attachments)
    state = _load_state(conversation_id)
    model_name = None
    if interaction:
        slot = _clean(interaction.get("slot"),64)
        if not slot: raise ValueError("interaction.slot 不能为空")
        state["slots"][slot] = interaction.get("value")
        state["turn_count"] += 1
    elif attachments and not message:
        state["primary_intent"] = "ingredient_analysis"
        state["turn_count"] += 1
    else:
        pending_step = state.get("current_step")
        extraction, model_name = _extract_intent(message, state)
        state["primary_intent"] = extraction["primary_intent"]
        state["secondary_intent"] = extraction.get("secondary_intent")
        state["slots"].update(extraction.get("slots") or {})
        if pending_step == "symptom_context" and message:
            state["slots"]["symptom_context"] = message
        state["turn_count"] += 1
    if attachments and not state.get("primary_intent"):
        state["primary_intent"] = "ingredient_analysis"
    context = _build_context(user_id, conversation, state, attachments)
    submission = context.get("food_submission")
    if submission and submission.get("recognition_status") in {"pending","processing"}:
        reply = {"response_type":"processing","reply":"配料表正在识别，完成后我会继续分析。",
                 "result":{"resource":"food_submission","id":submission["id"],"interval_ms":2000}}
    else:
        flow = _evaluate_flow(state, context)
        state["missing_slots"] = flow.get("missing_slots") or []
        if flow["status"] == "need_more_info":
            state["current_step"] = flow["next_slot"]
            state["followup_count"] += 1
            question = flow.get("question") or {}
            reply = {"response_type":question.get("response_type","text"),"reply":question.get("text","还需要补充一些信息。"),
                     "interaction":{"slot":flow["next_slot"],"options":question.get("options",[])}}
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
            text, answer_model = _generate_answer(message,state,context,flow["status"]=="answer_with_limited_info")
            model_name = answer_model or model_name
            reply = {"response_type":"result_card","reply":text,
                     "result":{"intent":state.get("primary_intent"),"slots":state.get("slots"),
                               "limited_info":flow["status"]=="answer_with_limited_info",
                               "food_candidates":context.get("food_candidates") or [],
                               "food_submission":submission},
                     "disclaimer":"AI内容仅供参考，不能替代兽医诊断"}
    _save_state(state)
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE {CONVERSATION_TABLE} SET primary_intent=%s,updated_at=%s WHERE id=%s AND user_id=%s", (state.get("primary_intent"),_now(),conversation_id,user_id))
        conn.commit()
    saved = _save_message(conversation_id=conversation_id,user_id=user_id,role="assistant",content=reply["reply"],
                          response_type=reply.get("response_type"),interaction=reply.get("interaction"),
                          result=reply.get("result"),model_name=model_name)
    saved["disclaimer"] = reply.get("disclaimer")
    return {"ok":True,"message":saved}
