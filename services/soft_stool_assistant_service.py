"""Application boundary for the soft-stool assistant."""

from __future__ import annotations

from typing import Any

from services.miniprogram_cat_profile_service import get_cat_profile
from soft_stool_assistant.pipeline import run_soft_stool_turn
from soft_stool_assistant.tools import search_products


MAX_MESSAGE_LENGTH = 4000


def _text(value: Any, max_length: int = 255) -> str:
    return str(value or "").strip()[:max_length]


def _diet_override(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("no_baseline") is True:
        return {"disabled": True}

    products = payload.get("baseline_products")
    if not isinstance(products, list):
        return None

    normalized = []
    for item in products[:10]:
        if not isinstance(item, dict):
            continue
        ref = _text(item.get("ref"), 255)
        if not ref:
            continue
        try:
            ratio = float(item.get("ratio", 1.0))
        except (TypeError, ValueError):
            raise ValueError("baseline_products.ratio 必须是数字")
        if ratio < 0 or ratio > 1:
            raise ValueError("baseline_products.ratio 必须在 0 到 1 之间")
        normalized.append({"ref": ref, "ratio": ratio})
    return {"products": normalized} if normalized else None


def handle_turn(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    pet_id = _text(payload.get("pet_id"), 64)
    message = _text(payload.get("message"), MAX_MESSAGE_LENGTH)
    if not pet_id:
        raise ValueError("pet_id 不能为空")
    if not message:
        raise ValueError("message 不能为空")

    # Ownership check. The assistant's data layer intentionally only accepts a
    # pet id, so authorization must be enforced at this application boundary.
    get_cat_profile(user_id, pet_id)

    state = payload.get("state")
    if state is not None and not isinstance(state, dict):
        raise ValueError("state 必须是 JSON 对象")
    if state and _text(state.get("pet_id"), 64) != pet_id:
        raise ValueError("state.pet_id 与 pet_id 不一致")

    result = run_soft_stool_turn(
        pet_id=pet_id,
        user_input=message,
        state=state,
        diet_override=_diet_override(payload),
        prefill=payload.get("prefill") is not False,
    )
    return {
        "ok": True,
        **result,
        "disclaimer": "内容仅用于日常护理参考，不能替代兽医诊断；出现便血、频繁呕吐、精神或食欲明显变差时请及时就医。",
    }


def find_products(keyword: Any, limit: Any = 15) -> dict[str, Any]:
    keyword = _text(keyword, 100)
    if not keyword:
        raise ValueError("q 不能为空")
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        raise ValueError("limit 必须是整数")
    return {"ok": True, "items": search_products(keyword, limit=limit)}
