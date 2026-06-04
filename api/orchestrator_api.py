"""Pipeline orchestrator API entrypoints."""

from __future__ import annotations

import json
from urllib import request as url_request
from urllib.error import HTTPError, URLError

from flask import Blueprint, current_app, jsonify, request

from services.orchestrator_service import (
    apply_manual_ocr_text,
    apply_node_result,
    cancel_task,
    claim_ready_dispatch_jobs,
    create_task,
    dispatch_scan,
    get_task,
    list_review_items,
    reset_node_for_reextract,
    run_task,
)


orchestrator_api = Blueprint("orchestrator_api", __name__, url_prefix="/api/orchestrator")


def _call_api_sync(api: dict, payload: dict) -> dict:
    method = str(api.get("method") or "POST").upper()
    url = str(api.get("url") or "").strip()
    timeout = int(api.get("timeout_seconds") or 30)
    if not url:
        raise ValueError("API URL 为空")

    if url.startswith("/"):
        with current_app.test_client() as client:
            response = client.open(url, method=method, json=payload)
            text = response.get_data(as_text=True)
            try:
                data = json.loads(text or "{}")
            except json.JSONDecodeError:
                data = {"raw": text}
            return {
                "status_code": response.status_code,
                "ok": 200 <= response.status_code < 300 and data.get("ok", True) is not False,
                "data": data,
            }

    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = url_request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method=method,
    )
    try:
        with url_request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8")
            try:
                data = json.loads(text or "{}")
            except json.JSONDecodeError:
                data = {"raw": text}
            return {
                "status_code": response.status,
                "ok": 200 <= response.status < 300 and data.get("ok", True) is not False,
                "data": data,
            }
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"status_code": exc.code, "ok": False, "data": {"error": detail}}
    except URLError as exc:
        return {"status_code": None, "ok": False, "data": {"error": str(exc.reason)}}


@orchestrator_api.post("/tasks")
def orchestrator_create_task():
    payload = request.get_json(silent=True) or {}
    task_type = str(payload.get("task_type") or "").strip()
    task_payload = dict(payload.get("payload") or {})
    auto_run = bool(payload.get("auto_run", False))
    try:
        task = create_task(task_type, task_payload)
        if auto_run:
            task = run_task(task["id"])
        return jsonify({"ok": True, "task": task}), 201
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.get("/tasks/<task_id>")
def orchestrator_get_task(task_id: str):
    try:
        task = get_task(task_id)
        if not task:
            return jsonify({"ok": False, "error": f"task_id 不存在: {task_id}"}), 404
        return jsonify({"ok": True, "task": task}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.get("/reviews")
def orchestrator_reviews():
    try:
        statuses = [
            status.strip()
            for status in str(request.args.get("statuses") or "").split(",")
            if status.strip()
        ]
        result = list_review_items(
            limit=int(request.args.get("limit") or 50),
            statuses=statuses or None,
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.post("/tasks/<task_id>/run")
def orchestrator_run_task(task_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        task = run_task(task_id, max_steps=int(payload.get("max_steps") or 100))
        return jsonify({"ok": True, "task": task}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.post("/tasks/<task_id>/cancel")
def orchestrator_cancel_task(task_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        task = cancel_task(task_id, reason=str(payload.get("reason") or "人工作废"))
        return jsonify({"ok": True, "task": task}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.post("/tasks/<task_id>/ocr-text")
def orchestrator_apply_manual_ocr_text(task_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        task = apply_manual_ocr_text(
            task_id,
            ocr_text=str(payload.get("ocr_text") or ""),
            reviewer=str(payload.get("reviewer") or "").strip() or None,
        )
        return jsonify({"ok": True, "task": task}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.post("/tasks/<task_id>/nodes/<node_code>/reextract")
def orchestrator_reextract_node(task_id: str, node_code: str):
    payload = request.get_json(silent=True) or {}
    try:
        task = reset_node_for_reextract(
            task_id,
            node_code,
            reason=str(payload.get("reason") or "人工触发重新抽取"),
        )
        return jsonify({"ok": True, "task": task}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.post("/tasks/<task_id>/nodes/<node_code>/result")
def orchestrator_apply_node_result(task_id: str, node_code: str):
    payload = request.get_json(silent=True) or {}
    try:
        task = apply_node_result(
            task_id,
            node_code,
            call_status=str(payload.get("call_status") or "success"),
            output=dict(payload.get("output") or {}),
            error_message=payload.get("error_message"),
        )
        return jsonify({"ok": True, "task": task}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.post("/dispatch-scan")
def orchestrator_dispatch_scan():
    payload = request.get_json(silent=True) or {}
    try:
        result = dispatch_scan(
            limit=int(payload.get("limit") or 20),
            task_type=str(payload.get("task_type") or "").strip() or None,
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.post("/dispatch-claim")
def orchestrator_dispatch_claim():
    payload = request.get_json(silent=True) or {}
    try:
        result = claim_ready_dispatch_jobs(
            limit=int(payload.get("limit") or 1),
            task_id=str(payload.get("task_id") or "").strip() or None,
            task_type=str(payload.get("task_type") or "").strip() or None,
            node_codes=[
                str(item).strip()
                for item in (payload.get("node_codes") or [])
                if str(item).strip()
            ],
            api_overrides=dict(payload.get("api_overrides") or {}),
            claim=True,
        )
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@orchestrator_api.post("/dispatch-call-sync")
def orchestrator_dispatch_call_sync():
    payload = request.get_json(silent=True) or {}
    try:
        dispatch_result = claim_ready_dispatch_jobs(
            limit=int(payload.get("limit") or 1),
            task_id=str(payload.get("task_id") or "").strip() or None,
            task_type=str(payload.get("task_type") or "").strip() or None,
            node_codes=[
                str(item).strip()
                for item in (payload.get("node_codes") or [])
                if str(item).strip()
            ],
            api_overrides=dict(payload.get("api_overrides") or {}),
            claim=True,
        )

        results = []
        for job in dispatch_result["jobs"]:
            if not job.get("api"):
                results.append({**job, "call_status": "api_not_configured"})
                continue

            api_result = _call_api_sync(job["api"], job["input"])
            call_status = "success" if api_result["ok"] else "failed"
            task = apply_node_result(
                job["task_id"],
                job["node_code"],
                call_status=call_status,
                output=api_result["data"] if isinstance(api_result["data"], dict) else {"result": api_result["data"]},
                error_message=None if api_result["ok"] else str(api_result["data"].get("error") or "API 调用失败"),
            )
            results.append(
                {
                    **job,
                    "call_status": call_status,
                    "api_result": api_result,
                    "task": task,
                }
            )
        return jsonify({"ok": True, "scanned": len(results), "results": results}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
