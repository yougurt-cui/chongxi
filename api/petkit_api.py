"""PETKIT（小佩）设备数据 API：饮水机/饭盆快照采集与查询。

全部接口走 admin_required（工作台登录或 X-Admin-Token）。只读采集，不操控设备。
"""

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services import petkit_service
from services.petkit_service import PetkitError


petkit_api = Blueprint("petkit_api", __name__, url_prefix="/api/petkit")


def _run(work, success_status=200):
    try:
        return jsonify(work()), success_status
    except PetkitError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@petkit_api.get("/devices")
@admin_required
def devices():
    """列出小佩账号下的所有设备（不写库）。"""
    return _run(lambda: {"ok": True, **petkit_service.list_devices()})


@petkit_api.post("/collect")
@admin_required
def collect():
    """采集一次设备快照。body: {"device_id": "166990", "hints": "water|feeder|逗号关键词"}"""
    payload = request.get_json(silent=True) or {}
    device_id = str(payload.get("device_id") or "").strip() or None
    hints = str(payload.get("hints") or "water").strip() or "water"
    return _run(lambda: {"ok": True, **petkit_service.collect(device_id=device_id, hints=hints)})


@petkit_api.get("/snapshots")
@admin_required
def snapshots():
    """查询本地已落库的快照记录。query: ?device_key=166990&limit=50"""
    return _run(lambda: {
        "ok": True,
        "snapshots": petkit_service.recent_snapshots(
            device_key=request.args.get("device_key"),
            limit=request.args.get("limit", 50),
        ),
    })


@petkit_api.get("/events")
@admin_required
def events():
    """实时采一次并返回事件级明细。query: ?device_id=388886&kind=eat|feed"""
    return _run(lambda: {
        "ok": True,
        **petkit_service.device_events(
            device_id=request.args.get("device_id"),
            kind=request.args.get("kind", "eat"),
        ),
    })
