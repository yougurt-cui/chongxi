"""Durable Alibaba Cloud Model Studio Batch jobs for historical comment distillation."""
from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI
from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Session, declarative_base

from app_config import get_qwen_config
from services import pet_knowledge_pipeline_service as pipeline

Base = declarative_base()
TERMINAL = {"completed", "failed", "expired", "cancelled"}
ACTIVE = {"submitted", "validating", "in_progress", "finalizing", "cancelling"}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BatchJob(Base):
    __tablename__ = "knowledge_batch_jobs"
    id = Column(String(32), primary_key=True)
    provider_batch_id = Column(String(128), unique=True)
    input_file_id = Column(String(128))
    output_file_id = Column(String(128))
    error_file_id = Column(String(128))
    model = Column(String(128), nullable=False)
    status = Column(String(24), nullable=False, default="preparing", index=True)
    request_count = Column(Integer, nullable=False, default=0)
    summary_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class BatchRequest(Base):
    __tablename__ = "knowledge_batch_requests"
    id = Column(Integer, primary_key=True)
    job_id = Column(String(32), nullable=False, index=True)
    custom_id = Column(String(256), nullable=False)
    post_id = Column(String(128), nullable=False)
    item_ids_json = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="queued", index=True)
    usage_json = Column(Text)
    error = Column(Text)
    __table_args__ = (UniqueConstraint("job_id", "custom_id", name="uq_knowledge_batch_request"),)


def ensure_tables(engine) -> None:
    Base.metadata.create_all(engine)


def _cfg(model: str | None = None) -> dict[str, str]:
    cfg = get_qwen_config({"model": model or "qwen3.7-flash"})
    if not cfg.get("api_key"):
        raise RuntimeError("缺少 DASHSCOPE_API_KEY/QWEN_API_KEY")
    return cfg


def _client(cfg: dict[str, str]) -> OpenAI:
    return OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60, max_retries=1)


def _select_chunks(engine, *, max_requests: int, batch_size: int,
                   per_post_cap: int) -> list[dict[str, Any]]:
    """Reserve neither rows nor money; return deterministic pending chunks for submit()."""
    with Session(engine) as db:
        queued_ids: set[int] = set()
        for raw, in db.execute(select(BatchRequest.item_ids_json).where(
                BatchRequest.status.in_(["queued", "submitted"]))):
            queued_ids.update(int(value) for value in pipeline.jl(raw, []))
        posts = db.scalars(select(pipeline.CommentItem.post_id).where(
            pipeline.CommentItem.status.in_(["pending", "failed"]),
            pipeline.CommentItem.is_noise == 0,
            pipeline.CommentItem.is_question == 0,
        ).distinct().order_by(pipeline.CommentItem.post_id)).all()
        chunks: list[dict[str, Any]] = []
        for post_id in posts:
            done_count = db.scalar(select(func.count()).select_from(pipeline.CommentItem).where(
                pipeline.CommentItem.post_id == post_id,
                pipeline.CommentItem.status == "done")) or 0
            allowance = None if not per_post_cap else max(0, per_post_cap - int(done_count))
            if allowance == 0:
                continue
            rows = db.scalars(select(pipeline.CommentItem).where(
                pipeline.CommentItem.post_id == post_id,
                pipeline.CommentItem.status.in_(["pending", "failed"]),
                pipeline.CommentItem.is_noise == 0,
                pipeline.CommentItem.is_question == 0,
            ).order_by(pipeline.CommentItem.id)).all()
            rows = [row for row in rows if row.id not in queued_ids]
            if allowance is not None:
                rows = rows[:allowance]
            if not rows:
                continue
            post = db.execute(pipeline.text(
                f"SELECT title,content FROM {pipeline.SOURCE_POST_TABLE} WHERE id=:pid"),
                {"pid": post_id}).mappings().first()
            for start in range(0, len(rows), batch_size):
                part = rows[start:start + batch_size]
                chunks.append({
                    "post_id": str(post_id), "item_ids": [row.id for row in part],
                    "payload": {
                        "post": {"id": str(post_id), "title": pipeline.clean(post["title"] if post else ""),
                                 "content": pipeline.clean(post["content"] if post else "")},
                        "comments": [{"id": row.comment_key, "text": row.cleaned_text} for row in part],
                        "allowed_topics": pipeline.TOPICS, "allowed_subtopics": pipeline.SUBTOPICS,
                    },
                })
                if len(chunks) >= max_requests:
                    return chunks
        return chunks


def _request_line(custom_id: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "custom_id": custom_id, "method": "POST", "url": "/v1/chat/completions",
        "body": {
            "model": model, "enable_thinking": False, "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": pipeline.COMMENT_EXTRACT_SYSTEM},
                {"role": "user", "content": pipeline.jd(payload)},
            ],
        },
    }


def submit(*, max_requests: int = 300, batch_size: int = 25, per_post_cap: int = 50,
           model: str = "qwen3.7-flash", engine=None, client=None) -> dict[str, Any]:
    if not 1 <= max_requests <= 50_000 or not 5 <= batch_size <= 50 or per_post_cap < 0:
        raise ValueError("invalid Batch limits")
    engine = engine or pipeline._engine()
    ensure_tables(engine)
    cfg = _cfg(model)
    client = client or _client(cfg)
    chunks = _select_chunks(engine, max_requests=max_requests, batch_size=batch_size,
                            per_post_cap=per_post_cap)
    if not chunks:
        return {"status": "nothing_to_submit", "requests": 0}
    job_id = uuid.uuid4().hex
    with Session(engine) as db:
        db.add(BatchJob(id=job_id, model=model, request_count=len(chunks), status="preparing"))
        for index, chunk in enumerate(chunks, 1):
            db.add(BatchRequest(job_id=job_id, custom_id=f"comment-{index}",
                                post_id=chunk["post_id"], item_ids_json=pipeline.jd(chunk["item_ids"])))
        db.commit()
    input_file_id = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", encoding="utf-8", delete=False) as handle:
            path = Path(handle.name)
            for index, chunk in enumerate(chunks, 1):
                handle.write(pipeline.jd(_request_line(f"comment-{index}", model, chunk["payload"])) + "\n")
        uploaded = client.files.create(file=path, purpose="batch")
        input_file_id = uploaded.id
        batch = client.batches.create(
            input_file_id=input_file_id, endpoint="/v1/chat/completions", completion_window="24h",
            metadata={"ds_name": f"chongxi-comments-{job_id[:8]}",
                      "ds_description": "Chongxi historical comment distillation"},
        )
        with Session(engine) as db:
            job = db.get(BatchJob, job_id)
            job.input_file_id, job.provider_batch_id, job.status = input_file_id, batch.id, "submitted"
            for request in db.scalars(select(BatchRequest).where(BatchRequest.job_id == job_id)):
                request.status = "submitted"
            db.commit()
        return {"status": "submitted", "job_id": job_id, "batch_id": batch.id,
                "input_file_id": input_file_id, "requests": len(chunks), "model": model}
    except Exception as exc:
        with Session(engine) as db:
            job = db.get(BatchJob, job_id)
            job.status = "failed"
            job.summary_json = pipeline.jd({"error_type": type(exc).__name__})
            db.commit()
        raise
    finally:
        if "path" in locals():
            path.unlink(missing_ok=True)


def _result_lines(client, file_id: str | None) -> list[dict[str, Any]]:
    if not file_id:
        return []
    content = client.files.content(file_id)
    raw = content.text if hasattr(content, "text") else content.read().decode("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _apply_completed(engine, client, job: BatchJob) -> dict[str, int]:
    outputs = {row.get("custom_id"): row for row in _result_lines(client, job.output_file_id)}
    errors = {row.get("custom_id"): row for row in _result_lines(client, job.error_file_id)}
    applied = failed = prompt_tokens = completion_tokens = 0
    with Session(engine) as db:
        requests = db.scalars(select(BatchRequest).where(BatchRequest.job_id == job.id)).all()
        for request in requests:
            row = outputs.get(request.custom_id)
            try:
                if not row or (row.get("response") or {}).get("status_code") != 200:
                    raise ValueError("missing or unsuccessful Batch response")
                body = row["response"]["body"]
                usage = body.get("usage") or {}
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)
                raw_points = json.loads(body["choices"][0]["message"]["content"]).get("knowledge_points", [])
                item_ids = [int(value) for value in pipeline.jl(request.item_ids_json, [])]
                items = db.scalars(select(pipeline.CommentItem).where(
                    pipeline.CommentItem.id.in_(item_ids))).all()
                by_key = {item.comment_key: item for item in items}
                valid, _rejected = pipeline._validate_comment_points(raw_points, by_key)
                merged_ids = pipeline.merge_items(db, request.post_id, valid)
                for item in items:
                    item.status, item.attempts = "done", item.attempts + 1
                for kid, valid_item in zip(merged_ids, valid):
                    for source_row in valid_item["source_rows"]:
                        target = by_key.get(source_row.comment_key)
                        if target is not None:
                            pipeline._add_vote(target, kid, valid_item["stance"], "origin",
                                               source_row.cleaned_text)
                request.status, request.usage_json, request.error = "completed", pipeline.jd(usage), None
                applied += 1
            except Exception as exc:
                request.status = "failed"
                request.error = pipeline.jd(errors.get(request.custom_id) or {
                    "error_type": type(exc).__name__})[:1500]
                failed += 1
        db.commit()
    return {"applied": applied, "failed": failed, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens}


def sync(*, engine=None, client=None, model: str = "qwen3.7-flash") -> list[dict[str, Any]]:
    engine = engine or pipeline._engine()
    ensure_tables(engine)
    cfg = _cfg(model)
    client = client or _client(cfg)
    results = []
    with Session(engine) as db:
        ids = [row.id for row in db.scalars(select(BatchJob).where(BatchJob.status.in_(ACTIVE))).all()]
    for job_id in ids:
        with Session(engine) as db:
            job = db.get(BatchJob, job_id)
            remote = client.batches.retrieve(batch_id=job.provider_batch_id)
            job.status = remote.status
            job.output_file_id = getattr(remote, "output_file_id", None)
            job.error_file_id = getattr(remote, "error_file_id", None)
            db.commit()
        summary: dict[str, Any] = {"job_id": job_id, "status": remote.status}
        if remote.status == "completed":
            with Session(engine) as db:
                detached = db.get(BatchJob, job_id)
                db.expunge(detached)
            summary.update(_apply_completed(engine, client, detached))
            with Session(engine) as db:
                row = db.get(BatchJob, job_id)
                row.summary_json = pipeline.jd(summary)
                db.commit()
            for file_id in filter(None, [detached.input_file_id, detached.output_file_id,
                                         detached.error_file_id]):
                try:
                    client.files.delete(file_id)
                except Exception:
                    pass
        elif remote.status in {"failed", "expired", "cancelled"}:
            with Session(engine) as db:
                for request in db.scalars(select(BatchRequest).where(
                        BatchRequest.job_id == job_id,
                        BatchRequest.status.in_(["queued", "submitted"]))):
                    request.status = "failed"
                    request.error = pipeline.jd({"batch_status": remote.status})
                db.commit()
        results.append(summary)
    return results


def run(**kwargs: Any) -> dict[str, Any]:
    synced = sync(model=kwargs.get("model", "qwen3.7-flash"))
    if any(item["status"] not in TERMINAL for item in synced):
        return {"status": "waiting", "synced": synced}
    submitted = submit(**kwargs)
    return {"status": submitted["status"], "synced": synced, "submitted": submitted}
