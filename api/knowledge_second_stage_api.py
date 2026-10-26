"""知识库二次蒸馏 API（kd_claims / kd_problem_guides / kd_topic_runs）。

- POST /run      对指定 topic/subtopic 的 knowledge_base 做二次蒸馏（指纹增量，无变更 skipped）
- GET  /guides   知识卡列表（可按 topic/subtopic/review_status 过滤）
- GET  /claims   论点明细（追溯原始知识 ID）
- GET  /stats    三表计数与状态分布
- POST /approve  人工审核知识卡（仅改变状态，专业已验证须专业人员执行）
"""

from flask import Blueprint, jsonify, request, session

from api.enterprise_api import admin_required
from services import knowledge_second_stage_service as distill


knowledge_second_stage_api = Blueprint(
    "knowledge_second_stage_api", __name__, url_prefix="/api/knowledge-second-stage",
)


def _run(work, success_status=200):
    try:
        return jsonify(work()), success_status
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@knowledge_second_stage_api.post("/run")
@admin_required
def run_distill():
    """二次蒸馏一个 topic/subtopic。

    body: {"topic"?: "日常喂养", "subtopic"?: "幼龄喂食", "mode"?: "llm"|"demo",
           "chunk"?: 20, "model"?}
    """
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "llm").strip()
    if mode not in {"llm", "demo"}:
        return jsonify({"ok": False, "error": "mode 只能是 llm 或 demo"}), 400
    return _run(lambda: {"ok": True, **distill.run_distill(
        topic=str(payload.get("topic") or "日常喂养"),
        subtopic=str(payload.get("subtopic") or "幼龄喂食"),
        mode=mode, chunk=int(payload.get("chunk") or 20), model=payload.get("model"),
    )})


@knowledge_second_stage_api.get("/guides")
@admin_required
def guides():
    """知识卡列表。query: topic / subtopic / review_status / limit / offset"""
    return _run(lambda: {"ok": True, "guides": distill.list_guides(
        topic=request.args.get("topic"), subtopic=request.args.get("subtopic"),
        review_status=request.args.get("review_status"),
        limit=int(request.args.get("limit") or 100), offset=int(request.args.get("offset") or 0),
    )})


@knowledge_second_stage_api.get("/claims")
@admin_required
def claims():
    """论点明细。query: topic / subtopic / problem_key / original_id / limit / offset"""
    original_id = request.args.get("original_id")
    return _run(lambda: {"ok": True, "claims": distill.list_claims(
        topic=request.args.get("topic"), subtopic=request.args.get("subtopic"),
        problem_key=request.args.get("problem_key"),
        original_id=int(original_id) if original_id and original_id.isdigit() else None,
        limit=int(request.args.get("limit") or 200), offset=int(request.args.get("offset") or 0),
    )})


@knowledge_second_stage_api.get("/stats")
@admin_required
def stats():
    """三表计数与知识卡状态分布。"""
    return _run(lambda: {"ok": True, **distill.stats()})


@knowledge_second_stage_api.post("/approve")
@admin_required
def approve():
    """人工审核知识卡。body: {"guide_id": 1, "evidence": "待验证|社区经验|专业已验证|有争议"}"""
    payload = request.get_json(silent=True) or {}
    evidence = str(payload.get("evidence") or "待验证")
    user = session.get("workbench_user") or {}
    if evidence == "专业已验证" and user.get("role") == "member":
        return jsonify({"ok": False, "error": "仅数据管理员或管理员令牌可标记专业已验证"}), 403
    guide_id = payload.get("guide_id")
    if not guide_id or not str(guide_id).isdigit():
        return jsonify({"ok": False, "error": "必须传数字 guide_id"}), 400
    return _run(lambda: {"ok": True, **distill.approve_guide(
        guide_id=int(guide_id), evidence=evidence,
    )})
