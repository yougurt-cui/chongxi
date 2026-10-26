"""宠物知识蒸馏管线 API（4 表结构：tasks / base / sources / comment_items）。

- POST /run      对 douyin_raw_content 按条件抽取知识（增量，指纹跳过未变化帖子，带同主题语义对齐 same_as_id）
- POST /verify   评论互证 + 反证校验，流转 evidence_status
- POST /dedup    离线语义去重：embedding 召回 + LLM 裁决 + 合并（支持 dry_run）
- POST /comment-tasks/prepare  清洗一篇帖子评论写入 knowledge_comment_items（幂等，不调模型）
- POST /comment-tasks/run      按 pending 条目动态切批蒸馏
- GET  /stats    四表计数 + 分布
- GET  /list     知识列表（可按 topic/subtopic/risk/evidence_status 过滤）
"""

from flask import Blueprint, jsonify, request

from api.enterprise_api import admin_required
from services import pet_knowledge_pipeline_service as pipeline


pet_knowledge_api = Blueprint("pet_knowledge_api", __name__, url_prefix="/api/pet-knowledge")


def _run(work, success_status=200):
    try:
        return jsonify(work()), success_status
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@pet_knowledge_api.post("/run")
@admin_required
def run_extract():
    """对来源帖子抽取知识。

    body: {"ingest_after"?: "2026-10-01 14:31:01", "keyword_pattern"?: "%养宠%",
           "extractor"?: "llm"|"demo", "limit"?: 0, "batch"?: 50, "model"?}
    增量：内容指纹未变化的帖子自动跳过。
    """
    payload = request.get_json(silent=True) or {}
    extractor = str(payload.get("extractor") or "llm").strip()
    if extractor not in {"llm", "demo"}:
        return jsonify({"ok": False, "error": "extractor 只能是 llm 或 demo"}), 400
    return _run(lambda: {"ok": True, **pipeline.run_extraction(
        ingest_after=payload.get("ingest_after"), keyword_pattern=payload.get("keyword_pattern"),
        extractor=extractor, batch=int(payload.get("batch") or 50),
        limit=int(payload.get("limit") or 0), model=payload.get("model"),
    )})


@pet_knowledge_api.post("/verify")
@admin_required
def verify():
    """评论互证校验；可按主题或 bucket_offset/max_buckets 分批执行。"""
    payload = request.get_json(silent=True) or {}
    return _run(lambda: {"ok": True, **pipeline.verify_evidence(
        model=payload.get("model"), claim_batch=int(payload.get("claim_batch") or 8),
        comment_batch=int(payload.get("comment_batch") or 80),
        comments_per_post=int(payload.get("comments_per_post") or 20),
        topic=payload.get("topic"), subtopic=payload.get("subtopic"),
        bucket_offset=int(payload.get("bucket_offset") or 0),
        max_buckets=int(payload.get("max_buckets") or 0),
    )})


@pet_knowledge_api.post("/comment-tasks/prepare")
@admin_required
def prepare_comment_tasks():
    """清洗评论写入 comment_items、分流提问，不调用模型；幂等可重复执行。"""
    payload = request.get_json(silent=True) or {}
    post_id = str(payload.get("post_id") or "").strip()
    if not post_id:
        return jsonify({"ok": False, "error": "必须传 post_id"}), 400
    return _run(lambda: {"ok": True, **pipeline.prepare_comment_tasks(
        post_id=post_id, batch_size=int(payload.get("batch_size") or 25),
        source_channel=str(payload.get("source_channel") or "douyin"),
    )})


@pet_knowledge_api.post("/comment-tasks/run")
@admin_required
def run_comment_tasks():
    """按 pending 评论动态切批调用千问；已 done 的评论自动跳过，失败批次可重跑。"""
    payload = request.get_json(silent=True) or {}
    post_id = str(payload.get("post_id") or "").strip()
    if not post_id:
        return jsonify({"ok": False, "error": "必须传 post_id"}), 400
    return _run(lambda: {"ok": True, **pipeline.run_comment_tasks(
        post_id=post_id, batch_size=int(payload.get("batch_size") or 25),
        max_tasks=int(payload.get("max_tasks") or 0), model=payload.get("model"),
    )})


@pet_knowledge_api.post("/dedup")
@admin_required
def dedup():
    """离线语义去重：同桶知识点 embedding 召回 + LLM 裁决 + 合并。

    body: {"min_sim"?: 0.85, "max_pairs"?: 200, "dry_run"?: false, "topics"?: ["日常喂养"], "model"?}
    建议 先 dry_run=true 看合并计划，确认后再执行。
    """
    payload = request.get_json(silent=True) or {}
    return _run(lambda: {"ok": True, **pipeline.dedup_knowledge(
        min_sim=float(payload.get("min_sim") or 0.85),
        max_pairs=int(payload.get("max_pairs") or 200),
        dry_run=payload.get("dry_run") is True,
        topics=payload.get("topics"), model=payload.get("model"),
    )})


@pet_knowledge_api.get("/stats")
@admin_required
def stats():
    """四表计数与分布。"""
    return _run(lambda: {"ok": True, **pipeline.stats()})


@pet_knowledge_api.get("/list")
@admin_required
def list_items():
    """知识列表。query: topic / subtopic / risk / evidence_status / limit / offset"""
    return _run(lambda: {"ok": True, "items": pipeline.list_knowledge(
        topic=request.args.get("topic"),
        subtopic=request.args.get("subtopic"),
        risk=request.args.get("risk"),
        evidence_status=request.args.get("evidence_status"),
        limit=int(request.args.get("limit") or 100),
        offset=int(request.args.get("offset") or 0),
    )})
