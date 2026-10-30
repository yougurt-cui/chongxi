"""Opt-in real MySQL queue/lock regression; owns and cleans every test row.

CHONGXI_RECALC_MYSQL=1; both configured databases must be localhost:13306.
No OCR/model calls and no existing formula, task or ingredient is modified.
"""
import os
import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(os.getenv('CHONGXI_RECALC_MYSQL') != '1', reason='local MySQL opt-in required')


@pytest.fixture
def case(monkeypatch):
    from app_config import get_mysql_config, get_feature_mysql_config
    from services import orchestrator_service as jobs, formula_calculation_service as calc
    from services import pipeline_recalculation_service as queue
    from services.catfood_standardization_service import build_formula_feature_input
    for cfg in (get_mysql_config(), get_feature_mysql_config()):
        assert cfg['host'] == '127.0.0.1' and cfg['port'] == 13306
    monkeypatch.setenv('CHONGXI_VERSIONED_CALCULATION', '1')
    monkeypatch.setenv('CHONGXI_PIPELINE_WORKER', '0')
    tag = 'qa-recalc-' + uuid.uuid4().hex
    brand_id = product_id = formula_id = task_id = None
    jobs.init_db()
    calc.migrate()
    try:
        with jobs._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT s.standard_name FROM catfood_standard_ingredient s
                    JOIN catfood_ingredient_science_profile p USING(standard_ingredient_id)
                    WHERE s.active=1 AND p.science_status='active' AND s.standard_name='鲜鸡肉' LIMIT 1""")
                seed = cur.fetchone()
                assert seed, 'local active science seed 鲜鸡肉 is required'
                brand_id = -(int(uuid.uuid4().hex[:7], 16) + 1000000000)
                cur.execute('INSERT INTO catfood_standard_brand(brand_id,standard_brand_name,active) VALUES(%s,%s,0)', (brand_id, tag))
                cur.execute('INSERT INTO catfood_standard_product(brand_id,standard_product_name,display_name,active) VALUES(%s,%s,%s,0)', (brand_id, tag, tag))
                product_id = cur.lastrowid
                cur.execute("""INSERT INTO catfood_standard_formula(product_id,normalized_ingredient_composition,
                    normalized_ingredients_json,ingredient_fingerprint,status,is_current,confirmation_status)
                    VALUES(%s,%s,%s,%s,'draft',0,'provisional')""",
                            (product_id, seed['standard_name'], calc.dumps([seed['standard_name']]), uuid.uuid4().hex))
                formula_id = cur.lastrowid
            conn.commit()
        build_formula_feature_input(formula_id=formula_id, apply=True)
        task = jobs.create_task('catfood_image_analysis', {'awaiting_submission_commit': True})
        task_id = task['id']
        with jobs._connect() as conn:
            for node in task['nodes']:
                jobs._set_node_status(conn, task_id, node['node_code'], jobs.NODE_SUCCESS)
            for code in ('formula_standardize', 'formula_input_build'):
                jobs._save_node_output(conn, task_id, code, {'formula_id': formula_id})
            jobs._save_node_output(conn, task_id, 'formula_profile', {'first_round': True})
            jobs._set_task_status(conn, task_id, jobs.TASK_DONE)
            conn.commit()
        yield {'task_id': task_id, 'formula_id': formula_id, 'tag': tag}
    finally:
        if formula_id:
            import pymysql
            with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT c.TABLE_NAME FROM information_schema.COLUMNS c
                        JOIN information_schema.TABLES t ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME
                        WHERE c.TABLE_SCHEMA=DATABASE() AND c.COLUMN_NAME='formula_id' AND t.TABLE_TYPE='BASE TABLE'""")
                    for row in cur.fetchall():
                        cur.execute(f"DELETE FROM `{row['TABLE_NAME']}` WHERE formula_id=%s", (formula_id,))
                conn.commit()
        with jobs._connect() as conn:
            with conn.cursor() as cur:
                if task_id:
                    for table in ('pipeline_recalculation_request', 'pipeline_node_output', 'pipeline_task_node', 'formula_calculation_task'):
                        cur.execute(f'DELETE FROM {table} WHERE task_id=%s', (task_id,))
                    cur.execute('DELETE FROM pipeline_task WHERE id=%s', (task_id,))
                if formula_id:
                    for table in ('catfood_formula_ingredient_item', 'catfood_formula_feature_profile', 'catfood_formula_feature_input',
                                  'formula_calculation_current', 'formula_calculation_run', 'formula_market_dirty', 'catfood_standard_formula'):
                        cur.execute(f'DELETE FROM {table} WHERE formula_id=%s', (formula_id,))
                if product_id:
                    cur.execute('DELETE FROM catfood_standard_product WHERE product_id=%s', (product_id,))
                if brand_id:
                    cur.execute('DELETE FROM catfood_standard_brand WHERE brand_id=%s', (brand_id,))
            conn.commit()


def pending(case):
    from services import orchestrator_service as jobs
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM pipeline_recalculation_request WHERE task_id=%s', (case['task_id'],))
            return cur.fetchone()


def confirm(case):
    from services import orchestrator_service as jobs
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE catfood_standard_formula SET confirmation_status='confirmed' WHERE formula_id=%s", (case['formula_id'],))
        conn.commit()


def test_real_queue_waits_for_round_and_strict_gate_then_restarts_once(case):
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    task_id = case['task_id']
    with jobs._connect() as conn:
        jobs._set_task_status(conn, task_id, jobs.TASK_PROCESSING)
        jobs._set_node_status(conn, task_id, 'formula_profile', jobs.NODE_RUNNING)
        conn.commit()
    queue.request(task_id)
    queue.request(task_id)
    row = pending(case)
    assert row['generation'] == 2
    assert queue._dispatch(row) == '等待当前流程结束'
    task = jobs.get_task(task_id)
    assert task['outputs']['formula_profile'] == {'first_round': True}
    assert next(n for n in task['nodes'] if n['node_code'] == 'formula_profile')['node_status'] == 'running'
    with jobs._connect() as conn:
        jobs._set_node_status(conn, task_id, 'formula_profile', jobs.NODE_SUCCESS)
        jobs._set_task_status(conn, task_id, jobs.TASK_DONE)
        conn.commit()
    assert queue._dispatch(row) == '产品、配方或原料身份尚未全部确认'
    assert jobs.get_task(task_id)['task_status'] == 'done'
    confirm(case)
    assert queue._dispatch(row) is None
    assert pending(case) is None
    task = jobs.get_task(task_id)
    assert task['task_status'] == 'processing'
    assert task['payload']['formal_calculation']
    assert 'formula_profile' not in task['outputs']
    assert next(n for n in task['nodes'] if n['node_code'] == 'formula_input_build')['node_status'] == 'ready'
    assert queue._dispatch(row) == '等待当前流程结束'


@pytest.mark.parametrize('lock_kind', ['task', 'formula'])
def test_real_worker_locks_block_reset_even_with_done_status(case, lock_kind):
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    from services.orchestrator_worker import lock_name
    confirm(case)
    queue.request(case['task_id'])
    name = lock_name(case['task_id']) if lock_kind == 'task' else f"formula-calculation:{case['formula_id']}"
    with jobs._connect() as owner:
        with owner.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s,0) AS acquired', (name,))
            assert cur.fetchone()['acquired']
        try:
            assert queue._dispatch(pending(case)) in {'当前流程仍在执行', '同一配方仍在计算'}
            assert jobs.get_task(case['task_id'])['outputs']['formula_profile'] == {'first_round': True}
        finally:
            with owner.cursor() as cur:
                cur.execute('SELECT RELEASE_LOCK(%s)', (name,))
    assert queue._dispatch(pending(case)) is None
    assert pending(case) is None


def test_concurrent_review_during_preflight_is_not_lost(case):
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    confirm(case)
    queue.request(case['task_id'])
    row = pending(case)
    real_gate = queue.formal_gate
    def edit_while_preparing(*args, **kwargs):
        result = real_gate(*args, **kwargs)
        queue.request(case['task_id'], 'brand_standardize', '新的身份审核')
        return result
    with patch.object(queue, 'formal_gate', side_effect=edit_while_preparing):
        assert queue._dispatch(row) == '审核信息已更新，等待重新检查'
    assert jobs.get_task(case['task_id'])['task_status'] == 'done'
    new = pending(case)
    assert new['generation'] == row['generation'] + 1
    assert new['node_code'] == 'brand_standardize'
    queue.request(case['task_id'], 'formula_input_build')
    assert pending(case)['node_code'] == 'brand_standardize'


def test_reset_failure_rolls_back_outputs_and_keeps_request(case):
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    confirm(case)
    queue.request(case['task_id'])
    real_reset = jobs._reset_task_nodes
    def interrupted(*args, **kwargs):
        real_reset(*args, **kwargs)
        raise RuntimeError('simulated crash before commit')
    with patch.object(jobs, '_reset_task_nodes', side_effect=interrupted):
        with pytest.raises(RuntimeError, match='simulated crash'):
            queue._dispatch(pending(case))
    assert pending(case)
    task = jobs.get_task(case['task_id'])
    assert task['task_status'] == 'done' and task['outputs']['formula_profile'] == {'first_round': True}
    assert queue._dispatch(pending(case)) is None


def test_cancelled_request_is_removed_without_restarting(case):
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    queue.request(case['task_id'])
    jobs.cancel_task(case['task_id'])
    assert queue._dispatch(pending(case)) is None
    assert pending(case) is None
    assert jobs.get_task(case['task_id'])['task_status'] == 'cancelled'


def test_review_blocked_first_upload_can_resume_provisional_round(case):
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    with jobs._connect() as conn:
        jobs._set_task_status(conn, case['task_id'], jobs.TASK_NEED_REVIEW)
        jobs._set_node_status(conn, case['task_id'], 'brand_standardize', jobs.NODE_NEED_REVIEW)
        conn.commit()
    queue.request(case['task_id'], 'brand_standardize')
    with patch.object(queue, 'formal_gate', side_effect=AssertionError('first round must remain provisional')):
        assert queue._dispatch(pending(case)) is None
    assert not jobs.get_task(case['task_id'])['payload']['formal_calculation']


def test_real_worker_publishes_official_after_gate(case):
    from main import app
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    from services import formula_calculation_service as calc
    from services.orchestrator_worker import _execute_task
    confirm(case)
    queue.request(case['task_id'])
    assert queue._dispatch(pending(case)) is None
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE pipeline_task SET payload_json=JSON_REMOVE(payload_json,'$.awaiting_submission_commit') WHERE id=%s", (case['task_id'],))
        conn.commit()
    _execute_task(app, case['task_id'])
    task = jobs.get_task(case['task_id'])
    assert task['task_status'] == 'done', [(n['node_code'], n['node_status'], n['error_message']) for n in task['nodes']]
    result = calc.published([case['formula_id']])[case['formula_id']]
    assert result['result_stage'] == 'official' and result['result_id']
    assert len(result['portrait']['dimensions']) == 6
    assert len(result['risks']) == 2


def test_admin_queue_exposes_wait_reason_and_excludes_cancelled(case):
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    # The queue listing uses real MySQL GROUP BY/joins, including a private draft.
    assert queue.formal_gate(case['formula_id'], refresh=True)
    queue.request(case['task_id'])
    queue._wait(pending(case), '等待当前流程结束')
    row = next(r for r in jobs.list_formula_formalization_items()['items'] if r['task_id'] == case['task_id'])
    assert row['recalculation_pending'] and not row['can_formalize']
    assert row['recalculation_message'] == '等待当前流程结束'
    jobs.cancel_task(case['task_id'])
    assert all(r['task_id'] != case['task_id'] for r in jobs.list_formula_formalization_items()['items'])


def test_formula_review_records_request_even_before_calculation_binding(case):
    from services import orchestrator_service as jobs, formula_calculation_service as calc
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO formula_calculation_current(formula_id,requested_run_id) VALUES(%s,1)', (case['formula_id'],))
        conn.commit()
    assert calc.requeue_formulas([case['formula_id']], invalidate=True) == [case['task_id']]
    assert pending(case)
    with jobs._connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT invalidated FROM formula_calculation_current WHERE formula_id=%s', (case['formula_id'],))
            assert cur.fetchone()['invalidated'] == 1


def test_identity_preparation_cannot_start_calculation_before_formal_gate(case):
    from main import app
    from services import orchestrator_service as jobs, pipeline_recalculation_service as queue
    from services import formula_calculation_service as calc
    from services.orchestrator_worker import _execute_task
    task_id = case['task_id']
    queue.request(task_id, 'brand_standardize', '品牌审核后需要生成新的产品候选')
    assert queue._dispatch(pending(case)) is None
    task = jobs.get_task(task_id)
    assert task['payload']['formal_calculation']
    assert next(n for n in task['nodes'] if n['node_code'] == 'brand_standardize')['node_status'] == 'ready'
    # Simulate completion of identity-only nodes, still pointing at a draft.
    with jobs._connect() as conn:
        for code in ('brand_standardize', 'formula_precheck', 'product_standardize', 'formula_standardize'):
            jobs._set_node_status(conn, task_id, code, jobs.NODE_SUCCESS)
        jobs._save_node_output(conn, task_id, 'formula_standardize', {'formula_id': case['formula_id']})
        jobs._set_node_status(conn, task_id, 'formula_input_build', jobs.NODE_READY)
        with conn.cursor() as cur:
            cur.execute("UPDATE pipeline_task SET payload_json=JSON_REMOVE(payload_json,'$.awaiting_submission_commit') WHERE id=%s", (task_id,))
        conn.commit()
    _execute_task(app, task_id)
    assert jobs.get_task(task_id)['task_status'] == 'need_review'
    assert calc.task_run(task_id) is None  # No second calculation version, let alone scores.
    assert pending(case)['node_code'] == 'formula_input_build'
    # This is a formal round, not a first-upload recovery: it must keep waiting.
    assert queue._dispatch(pending(case)) == '产品、配方或原料身份尚未全部确认'
    confirm(case)
    assert queue._dispatch(pending(case)) is None
