"""健康关注引擎 API（6 张表 + 小佩设备桥接）。

- POST /metrics            人工/外部录入归一化指标
- POST /analyze            跑健康方向分析
- GET  /attention/<pet_id> 读最近一次的 5 类卡片
- POST /collect-and-analyze 采小佩快照 → 落日指标 → 跑分析（与 /api/petkit 打通）
"""

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services import pet_health_engine_service as engine
from services.petkit_service import PetkitError


pet_health_api = Blueprint("pet_health_api", __name__, url_prefix="/api/pet-health")


def _run(work, success_status=200):
    try:
        return jsonify(work()), success_status
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except PetkitError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@pet_health_api.post("/metrics")
@admin_required
def add_metric():
    """录入一条归一化指标。body: {"pet_id","metric_code","metric_value","unit","record_time"?}"""
    payload = request.get_json(silent=True) or {}
    return _run(lambda: {"ok": True, **engine.add_metric_api(payload)}, 201)


@pet_health_api.post("/analyze")
@admin_required
def analyze():
    """跑健康方向分析。body: {"pet_id":"xxx","target_date":"2026-10-23"(可选,默认今天)}"""
    payload = request.get_json(silent=True) or {}
    pet_id = str(payload.get("pet_id") or "").strip()
    if not pet_id:
        return jsonify({"ok": False, "error": "必须传 pet_id"}), 400
    return _run(lambda: {"ok": True, **engine.analyze_pet(pet_id, payload.get("target_date"))})


@pet_health_api.get("/attention/<pet_id>")
@admin_required
def attention(pet_id: str):
    """读取该宠物最近一次分析的 5 类健康方向卡片。"""
    return _run(lambda: {"ok": True, **engine.latest_attention(pet_id)})


@pet_health_api.post("/collect-and-analyze")
@admin_required
def collect_and_analyze():
    """小佩设备采集 + 健康分析一条龙。
    body: {"pet_id":"xxx","device_id":"388886"(喂食器key),"target_date"?(默认今天),
           "attribution_status"?(默认shared；确认单宠使用时可传confirmed)}
    """
    payload = request.get_json(silent=True) or {}
    pet_id = str(payload.get("pet_id") or "").strip()
    device_id = str(payload.get("device_id") or "").strip()
    if not pet_id or not device_id:
        return jsonify({"ok": False, "error": "必须传 pet_id 和 device_id"}), 400
    return _run(lambda: {"ok": True, **engine.collect_and_analyze(
        pet_id, device_id, payload.get("target_date"), payload.get("attribution_status", "shared")
    )})
