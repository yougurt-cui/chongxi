"""Bounded in-process dispatch, durable MySQL jobs, and best-effort wakeups.

Interrupted HTTP/business calls are *not* blindly replayed: their side effects
may have committed. Mark them for inspection, retaining completed node outputs.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor

from services import orchestrator_service as jobs

log = logging.getLogger(__name__)
_wake = threading.Event()
_stop = threading.Event()
_start_lock = threading.Lock()
_thread = None


def wake():
    _wake.set()


def lock_name(task_id):
    return "pipeline-worker:" + task_id


def _candidates(limit):
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT t.id FROM pipeline_task t
                WHERE t.task_type='catfood_image_analysis'
                  AND t.task_status IN ('created','processing','waiting_input')
                  AND COALESCE(JSON_EXTRACT(t.payload_json,'$.awaiting_submission_commit'),false)=false
                  AND EXISTS (SELECT 1 FROM pipeline_task_node n WHERE n.task_id=t.id
                    AND (n.node_status='ready' OR (t.task_status='created' AND n.node_code='upload_check' AND n.node_status='pending'))
                    AND (n.next_attempt_at IS NULL OR n.next_attempt_at<=UTC_TIMESTAMP()))
                ORDER BY t.created_at,t.id LIMIT %s""", (limit,))
            return [r["id"] for r in cur.fetchall()]


def recover_interrupted():
    """An absent advisory lock proves the previous worker lost ownership.

    It does not prove its business request had no side effects, so do not replay.
    """
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT task_id,node_code,claim_token FROM pipeline_task_node "
                        "WHERE node_status='running' AND claim_token LIKE 'worker:%%'")
            rows = list(cur.fetchall())
    for row in rows:
        with jobs._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name(row["task_id"]),))
                if not cur.fetchone()["acquired"]:
                    continue
                try:
                    cur.execute("UPDATE pipeline_task_node SET node_status='need_review',claim_token=NULL,"
                                "error_message=%s,updated_at=UTC_TIMESTAMP() "
                                "WHERE task_id=%s AND node_code=%s AND claim_token=%s AND node_status='running'",
                                ("执行中断，请核验已有结果后重跑本节点；已完成节点保留", row["task_id"], row["node_code"], row["claim_token"]))
                    if cur.rowcount:
                        jobs._set_task_status(conn, row["task_id"], jobs.TASK_NEED_REVIEW,
                                              "执行中断，等待核验；没有自动重放可能已产生结果的请求")
                    conn.commit()
                finally:
                    cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name(row["task_id"]),))


def retryable(response):
    # Only explicit rejections are known not to have performed business work.
    # A generic 5xx/timeout can occur *after* commit and is not safe to replay.
    data = response.get("data") or {}
    return response.get("status_code") == 429 or data.get("code") == "market_profile_busy"


def _defer(job, response):
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT attempt_count FROM pipeline_task_node WHERE task_id=%s AND node_code=%s AND claim_token=%s FOR UPDATE",
                        (job["task_id"], job["node_code"], job["claim_token"]))
            row = cur.fetchone()
            if not row:
                return
            attempts = int(row["attempt_count"])
            if attempts >= 3:
                jobs._set_node_status(conn, job["task_id"], job["node_code"], jobs.NODE_NEED_REVIEW, "自动重试已达 3 次，请稍后核验重跑")
                jobs._set_task_status(conn, job["task_id"], jobs.TASK_NEED_REVIEW, "重试次数达到上限")
            else:
                cur.execute("UPDATE pipeline_task_node SET node_status='ready',claim_token=NULL,"
                            "next_attempt_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL %s SECOND),error_message=%s "
                            "WHERE task_id=%s AND node_code=%s AND claim_token=%s",
                            (15 * 2 ** (attempts - 1), "服务暂忙，已安排有限重试", job["task_id"], job["node_code"], job["claim_token"]))
            conn.commit()


def _wait_for_formal_review(task_id, node_code, reason, *, claim_token=None):
    from services.pipeline_recalculation_service import request
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT task_status FROM pipeline_task WHERE id=%s FOR UPDATE', (task_id,))
            task = cur.fetchone()
            if not task or task['task_status'] == jobs.TASK_CANCELLED:
                return False
            cur.execute('SELECT node_status,claim_token FROM pipeline_task_node WHERE task_id=%s AND node_code=%s FOR UPDATE',
                        (task_id, node_code))
            node = cur.fetchone() or {}
            if (claim_token is not None and node.get('claim_token') != claim_token
                    or claim_token is None and node.get('node_status') != jobs.NODE_READY):
                return False
        jobs._set_node_status(conn, task_id, node_code, jobs.NODE_NEED_REVIEW, reason)
        jobs._set_task_status(conn, task_id, jobs.TASK_NEED_REVIEW, reason)
        conn.commit()
    request(task_id, reason=reason)
    return True


def _execute_task(app, task_id):
    with app.app_context(), jobs._connect() as owner, ExitStack() as calculations_scope:
        with owner.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name(task_id),))
            if not cur.fetchone()["acquired"]:
                return
        try:
            # The owning connection stays open while the request runs. A helper
            # keeps it alive through long OCR/subprocess calls.
            finished = threading.Event()
            def heartbeat():
                while not finished.wait(15):
                    try:
                        owner.ping(reconnect=False)
                    except Exception:
                        log.exception("pipeline_owner_connection_lost task=%s", task_id)
                        return
            keeper = threading.Thread(target=heartbeat, daemon=True)
            keeper.start()
            try:
                from api.orchestrator_api import _call_api_sync
                locked_formula = None
                stale_restarts = 0
                for _ in range(24):
                    if _stop.is_set():
                        break
                    task = jobs.get_task(task_id)
                    if not task or task["task_status"] in {"done", "cancelled", "failed", "need_review"}:
                        break
                    from services.formula_calculation_service import enabled, formula_lock
                    formula_id = ((task.get('outputs') or {}).get('formula_standardize') or {}).get('formula_id')
                    if enabled() and formula_id and locked_formula != int(formula_id):
                        if not calculations_scope.enter_context(formula_lock(formula_id)):
                            break  # Leave the durable ready node unclaimed for a later sweep.
                        locked_formula = int(formula_id)
                    # Identity metadata may need rebuilding after brand review.
                    # Gate BEFORE creating a new calculation run, then recheck
                    # before science/scoring in case another review arrived.
                    formal_boundary = next((n['node_code'] for n in task.get('nodes', [])
                                            if n['node_status'] == 'ready' and n['node_code'] in
                                            {'formula_input_build', 'science_feature_materialize'}), None)
                    if task.get('payload', {}).get('formal_calculation') and formula_id and formal_boundary:
                        from services import pipeline_recalculation_service as requests
                        blocker = requests.formal_gate(int(formula_id), refresh=formal_boundary == 'formula_input_build')
                        if blocker:
                            _wait_for_formal_review(task_id, formal_boundary, blocker)
                            break
                    ready = jobs.claim_ready_dispatch_jobs(task_id=task_id, limit=1, claim_prefix="worker:")
                    if not ready["jobs"]:
                        break
                    job = ready["jobs"][0]
                    if job["node_code"] == "upload_check" and not job["api"]:
                        jobs.apply_node_result(task_id, "upload_check", call_status="success", output=task["payload"],
                                               claim_token=job["claim_token"])
                        continue
                    started = time.monotonic()
                    response = _call_api_sync(job["api"], job["input"])
                    log.info("pipeline_node_finished task=%s node=%s seconds=%.3f ok=%s",
                             task_id, job["node_code"], time.monotonic() - started, response["ok"])
                    if (response.get('data') or {}).get('code') == 'calculation_superseded' and stale_restarts < 1:
                        from services import pipeline_recalculation_service as requests
                        if task.get('payload', {}).get('formal_calculation') or requests.has_pending(task_id):
                            _wait_for_formal_review(task_id, job['node_code'], '审核信息更新，等待正式门禁通过',
                                                    claim_token=job['claim_token'])
                            break
                        stale_restarts += 1
                        jobs.reset_node_for_reextract(task_id, 'formula_input_build', '输入或参考池更新，重新计算')
                        continue
                    if not response["ok"] and retryable(response):
                        _defer(job, response)
                        break
                    review_required = (response.get("data") or {}).get("status") in {"need_review", "science_features_not_ready"}
                    jobs.apply_node_result(task_id, job["node_code"],
                        call_status="success" if response["ok"] or review_required else "failed",
                        output=response.get("data") or {},
                        error_message=None if response["ok"] else str((response.get("data") or {}).get("error") or "节点执行失败"),
                        claim_token=job["claim_token"])
            finally:
                finished.set()
                keeper.join(timeout=2)
        except Exception:
            log.exception("pipeline_task_execution_error task=%s", task_id)
        finally:
            try:
                from services.miniprogram_food_submission_service import _sync_recognition
                with jobs._connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT * FROM miniprogram_food_submission WHERE orchestrator_task_id=%s AND status='active'", (task_id,))
                        submissions = list(cur.fetchall())
                for submission in submissions:
                    _sync_recognition(submission)
            except Exception:
                log.exception("pipeline_submission_sync_failed task=%s", task_id)
            with owner.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name(task_id),))
            wake()


def _loop(app, capacity, interval):
    pending = {}
    with ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="food-pipeline") as pool:
        while not _stop.is_set():
            _wake.clear()
            try:
                with app.app_context():
                    jobs.init_db()
                    recover_interrupted()
                    from services.formula_calculation_service import reconcile_dependencies
                    reconcile_dependencies()
                    from services.pipeline_recalculation_service import reconcile as reconcile_recalculations
                    reconcile_recalculations()
                    pending = {k: v for k, v in pending.items() if not v.done()}
                    for task_id in _candidates(capacity * 4):
                        if len(pending) >= capacity:
                            break
                        if task_id not in pending:
                            pending[task_id] = pool.submit(_execute_task, app, task_id)
                    from services.submission_notifications import reconcile
                    reconcile()
            except Exception:
                log.exception("pipeline_dispatch_scan_failed")
            _wake.wait(interval)


def start(app):
    """Explicit opt-in avoids consuming queues in tests, CLI imports or reloaders."""
    global _thread
    if os.getenv("CHONGXI_PIPELINE_WORKER", "0") != "1":
        return
    if os.getenv("FLASK_DEBUG") == "1" and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return
    with _start_lock:
        if _thread and _thread.is_alive():
            return
        capacity = max(1, min(int(os.getenv("CHONGXI_PIPELINE_CONCURRENCY", "1")), 4))
        interval = max(5, int(os.getenv("CHONGXI_PIPELINE_SWEEP_SECONDS", "60")))
        _stop.clear()
        _thread = threading.Thread(target=_loop, args=(app, capacity, interval), daemon=True, name="pipeline-dispatch")
        _thread.start()


def stop():
    _stop.set()
    wake()
