"""Durable, coalesced review requests. Never reset an executing pipeline.

The worker's task/formula advisory locks protect preparation; a task-row lock
and generation check make reset + acknowledgement one atomic transaction.
"""
from __future__ import annotations

import logging

from services import orchestrator_service as jobs

log = logging.getLogger(__name__)
RESTART_NODES = {"brand_standardize": 0, "formula_input_build": 1}
STOPPED = {jobs.TASK_DONE, jobs.TASK_NEED_REVIEW, jobs.TASK_FAILED}


def task_formula_id(task):
    outputs = task.get('outputs') or {}
    for code in ('formula_input_build', 'formula_standardize', 'product_standardize'):
        value = (outputs.get(code) or {}).get('formula_id')
        if value:
            return int(value)
    return None


def request_many(task_ids, node_code='formula_input_build', reason='审核更新，等待正式重算', *, invalidate_formula_ids=()):
    """Record intent and hide outdated publications in the same transaction."""
    if node_code not in RESTART_NODES:
        raise ValueError('不支持的重算起点')
    jobs.init_db()
    accepted = []
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            for task_id in sorted(set(task_ids)):
                cur.execute('SELECT task_type,task_status FROM pipeline_task WHERE id=%s FOR UPDATE', (task_id,))
                task = cur.fetchone()
                if not task or task['task_type'] != 'catfood_image_analysis' or task['task_status'] == jobs.TASK_CANCELLED:
                    continue
                # Preserve an earlier identity restart when ingredient edits arrive.
                cur.execute("""INSERT INTO pipeline_recalculation_request(task_id,node_code,reason,next_check_at,updated_at)
                    VALUES(%s,%s,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE
                    node_code=IF(node_code='brand_standardize',node_code,VALUES(node_code)),
                    generation=generation+1,reason=VALUES(reason),waiting_reason=NULL,
                    next_check_at=UTC_TIMESTAMP(),updated_at=UTC_TIMESTAMP()""", (task_id, node_code, reason))
                accepted.append(task_id)
            ids = sorted({int(v) for v in invalidate_formula_ids})
            if ids:
                cur.execute(f"UPDATE formula_calculation_current SET invalidated=1 WHERE formula_id IN ({','.join(['%s'] * len(ids))})", ids)
        conn.commit()
    if accepted:
        jobs.wake_dispatcher()
    return accepted


def request(task_id, node_code='formula_input_build', reason='审核更新，等待正式重算'):
    if not request_many([task_id], node_code, reason):
        raise ValueError('投稿任务不存在或已撤销，不安排重算')


def has_pending(task_id):
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT task_id FROM pipeline_recalculation_request WHERE task_id=%s', (task_id,))
            return bool(cur.fetchone())


def round_finished(task):
    return (task['task_status'] in STOPPED
            and not any(n['node_status'] in {jobs.NODE_RUNNING, jobs.NODE_WAITING_RESULT}
                        for n in task['nodes']))


def target_formula_id(task):
    """An approved identity may have moved the source to another formula."""
    source_id = (task.get('outputs', {}).get('ocr_formula') or {}).get('source_id')
    if source_id:
        with jobs._connect() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT formula_id FROM catfood_ocr_standard_mapping WHERE source_id=%s', (source_id,))
                row = cur.fetchone()
                if row and row.get('formula_id'):
                    return int(row['formula_id'])
    return task_formula_id(task)


def formal_gate(formula_id, *, refresh=False):
    """Rebuild only input/standardization metadata, never scores or risk results.

    Alias reviews do not refresh the materialized profile. Checking that stale
    profile alone would leave an otherwise approved formula waiting forever.
    Call refresh only while the formula and task are idle and locked.
    """
    if refresh:
        from services.catfood_standardization_service import build_formula_feature_input
        from scripts.backfill_formula_ingredient_features import backfill_formula
        build_formula_feature_input(formula_id=formula_id, apply=True)
        backfill_formula(formula_id, apply=True, materialize_science_features=False)
        build_formula_feature_input(formula_id=formula_id, apply=True)
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT f.confirmation_status,p.overall_status,
                p.unmatched_ingredient_count,i.result_stage
                FROM catfood_standard_formula f
                LEFT JOIN catfood_formula_feature_profile p ON p.formula_id=f.formula_id
                LEFT JOIN catfood_formula_feature_input i ON i.formula_id=f.formula_id
                WHERE f.formula_id=%s""", (formula_id,))
            row = cur.fetchone() or {}
            if (row.get('confirmation_status') != 'confirmed'
                    or row.get('result_stage') != 'official'):
                return '产品、配方或原料身份尚未全部确认'
            if row.get('overall_status') != 'ready_for_rebuild' or row.get('unmatched_ingredient_count') != 0:
                return '严格原料门禁尚未通过'
            cur.execute("""SELECT COUNT(*) AS total,
                SUM(CASE WHEN s.standard_ingredient_id IS NULL OR s.active<>1
                    OR p.science_status IS NULL OR p.science_status<>'active' THEN 1 ELSE 0 END) AS missing
                FROM catfood_formula_ingredient_item i
                LEFT JOIN catfood_standard_ingredient s ON s.standard_ingredient_id=i.standard_ingredient_id
                LEFT JOIN catfood_ingredient_science_profile p ON p.standard_ingredient_id=i.standard_ingredient_id
                WHERE i.formula_id=%s AND COALESCE(i.is_ignored,0)=0""", (formula_id,))
            science = cur.fetchone()
            if not science['total'] or science['missing']:
                return '原料科学属性尚未全部审核通过'
    return None


def _wait(request_row, reason):
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE pipeline_recalculation_request SET waiting_reason=%s,
                next_check_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL 60 SECOND)
                WHERE task_id=%s AND generation=%s""", (reason, request_row['task_id'], request_row['generation']))
        conn.commit()


def _dispatch(row):
    from services import formula_calculation_service as calc
    from services.orchestrator_worker import lock_name
    task_id = row['task_id']
    with jobs._connect() as owner:
        locks = []
        try:
            def acquire(name):
                with owner.cursor() as cur:
                    cur.execute('SELECT GET_LOCK(%s,0) AS acquired', (name,))
                    if not cur.fetchone()['acquired']:
                        return False
                locks.append(name)
                return True

            if not acquire(lock_name(task_id)):
                return '当前流程仍在执行'
            task = jobs.get_task(task_id)
            if not task or task['task_status'] == jobs.TASK_CANCELLED:
                with owner.cursor() as cur:
                    cur.execute('DELETE FROM pipeline_recalculation_request WHERE task_id=%s AND generation=%s',
                                (task_id, row['generation']))
                owner.commit()
                return None
            if not round_finished(task):
                return '等待当前流程结束'
            if str(task.get('error_message') or '').startswith('执行中断'):
                return '执行中断，需先核验已有结果后恢复任务'

            # A review-blocked upload with no calculation yet must be allowed to
            # resume its FIRST provisional round, otherwise strict gating here
            # would deadlock the initial flow.
            initial_resume = (task['task_status'] == jobs.TASK_NEED_REVIEW
                              and not task.get('payload', {}).get('formal_calculation')
                              and calc.task_run(task_id) is None)
            formula_id = target_formula_id(task)
            if formula_id and not acquire(f'formula-calculation:{formula_id}'):
                return '同一配方仍在计算'
            # Brand approval clears the source's product/formula mapping. Let
            # identity-only nodes rebuild review candidates first; the worker
            # gates formula_input_build BEFORE creating any calculation run.
            # Requiring the old draft's approval here would deadlock that flow.
            prepare_identity = row['node_code'] == 'brand_standardize' and not initial_resume
            if not initial_resume and not prepare_identity:
                if not formula_id:
                    return '等待确认配方身份'
                blocker = formal_gate(formula_id, refresh=True)
                if blocker:
                    return blocker

            with owner.cursor() as cur:
                # Same ordering as request()/node claiming/cancellation.
                cur.execute('SELECT task_status FROM pipeline_task WHERE id=%s FOR UPDATE', (task_id,))
                current = cur.fetchone()
                cur.execute('SELECT generation FROM pipeline_recalculation_request WHERE task_id=%s FOR UPDATE', (task_id,))
                latest = cur.fetchone()
                if not latest or latest['generation'] != row['generation']:
                    return '审核信息已更新，等待重新检查'
                if not current or current['task_status'] != task['task_status']:
                    return '任务状态已变化，等待重新检查'
                cur.execute("SELECT node_status FROM pipeline_task_node WHERE task_id=%s FOR UPDATE", (task_id,))
                if any(n['node_status'] in {jobs.NODE_RUNNING, jobs.NODE_WAITING_RESULT} for n in cur.fetchall()):
                    return '当前流程仍有运行中的节点'
                jobs._reset_task_nodes(owner, task, row['node_code'], row['reason'])
                cur.execute("UPDATE pipeline_task SET payload_json=JSON_SET(payload_json,'$.formal_calculation',%s) WHERE id=%s",
                            (not initial_resume, task_id))
                cur.execute('DELETE FROM pipeline_recalculation_request WHERE task_id=%s AND generation=%s',
                            (task_id, row['generation']))
            owner.commit()
            return None
        finally:
            owner.rollback()
            with owner.cursor() as cur:
                for name in reversed(locks):
                    cur.execute('SELECT RELEASE_LOCK(%s)', (name,))


def reconcile(limit=20):
    from services.formula_calculation_service import enabled
    if not enabled():
        return
    jobs.init_db()
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT * FROM pipeline_recalculation_request
                WHERE next_check_at<=UTC_TIMESTAMP() ORDER BY next_check_at,task_id LIMIT %s""", (limit,))
            rows = list(cur.fetchall())
    for row in rows:
        try:
            reason = _dispatch(row)
        except Exception as exc:
            log.exception('pipeline_recalculation_check_failed task=%s', row['task_id'])
            reason = f'重算准备失败，稍后重试：{exc}'
        if reason:
            _wait(row, reason)
