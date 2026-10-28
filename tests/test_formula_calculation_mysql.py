"""Opt-in local-MySQL regression with real scoring subprocesses.

CHONGXI_QA38_MYSQL=1 .venv/bin/python -m pytest -q tests/test_formula_calculation_mysql.py -s
Requires the existing local 13306 QA database, with OCR source 392 and science data.
Only the newly created fixture is removed; no production connection is accepted.
"""
import json
import os
import time
import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(os.getenv('CHONGXI_QA38_MYSQL') != '1', reason='explicit local MySQL opt-in required')


@pytest.fixture
def calculation_case(monkeypatch):
    from app_config import get_mysql_config, get_feature_mysql_config
    for cfg in (get_mysql_config(), get_feature_mysql_config()):
        assert cfg['host'] == '127.0.0.1' and int(cfg['port']) == 13306
    monkeypatch.setenv('CHONGXI_VERSIONED_CALCULATION', '1')
    monkeypatch.setenv('CHONGXI_PIPELINE_WORKER', '0')
    from services import formula_calculation_service as calc, orchestrator_service as jobs
    from services.catfood_standardization_service import init_standardization_db
    import pymysql
    init_standardization_db();calc.migrate()
    sid = 380000000000 + int(uuid.uuid4().hex[:9], 16)
    tag = f'QA38-{sid}'
    task_id = None
    try:
        with calc.connect() as conn:
            with conn.cursor() as c:
                c.execute('SELECT * FROM catfood_ingredient_ocr_parsed WHERE source_id=392 LIMIT 1')
                row = c.fetchone();assert row, 'local OCR seed 392 is required'
                row.pop('id')
                row.update(source_id=sid, brand=tag, product_name=tag, file_sha256=uuid.uuid4().hex*2,
                           parse_batch_id='qa38', image_group_key=tag, merged_source_ids=str(sid), image_name=tag+'.jpg')
                keys = list(row)
                c.execute('INSERT INTO catfood_ingredient_ocr_parsed ('+','.join('`'+k+'`' for k in keys)+') VALUES ('+','.join(['%s']*len(keys))+')', [row[k] for k in keys])
                parsed_id = c.lastrowid
            conn.commit()
        task = jobs.create_task('catfood_image_analysis', {'brand_name': tag, 'product_name': tag, 'awaiting_submission_commit': True})
        task_id = task['id']
        with calc.connect() as conn:
            jobs._save_node_output(conn, task_id, 'ocr_formula', {**row, 'parsed_row_id': parsed_id})
            for code in ('upload_check', 'ocr_formula'):
                jobs._set_node_status(conn, task_id, code, jobs.NODE_SUCCESS, None)
            jobs._set_task_status(conn, task_id, jobs.TASK_PROCESSING)
            with conn.cursor() as c:
                c.execute("UPDATE pipeline_task SET payload_json=JSON_REMOVE(payload_json,'$.awaiting_submission_commit') WHERE id=%s", (task_id,))
            conn.commit()
        jobs._refresh_ready_nodes(jobs.get_task(task_id))
        yield sid, task_id
    finally:
        # Discover only this test's private identity before removing its lineage.
        with calc.connect() as conn:
            with conn.cursor() as c:
                c.execute('SELECT * FROM formula_calculation_draft WHERE source_id=%s', (sid,))
                draft = c.fetchone()
                c.execute("SELECT DISTINCT task_id FROM pipeline_node_output WHERE node_code='ocr_formula' AND JSON_EXTRACT(output_json,'$.source_id')=%s", (sid,))
                task_ids = {r['task_id'] for r in c.fetchall()} | ({task_id} if task_id else set())
        for cfg in (get_feature_mysql_config(), get_mysql_config()):
            with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor) as conn:
                with conn.cursor() as c:
                    c.execute("SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND COLUMN_NAME IN ('formula_id','source_id')")
                    columns = list(c.fetchall())
                    for column in columns:
                        key = column['COLUMN_NAME']; value = draft['formula_id'] if key == 'formula_id' and draft else sid if key == 'source_id' else None
                        if value is not None:
                            c.execute(f"DELETE FROM `{column['TABLE_NAME']}` WHERE `{key}`=%s", (value,))
                    if cfg['database'] == get_mysql_config()['database']:
                        if draft:
                            c.execute('DELETE FROM catfood_standard_product WHERE product_id=%s', (draft['product_id'],))
                            c.execute('DELETE FROM catfood_standard_brand WHERE brand_id=%s', (draft['brand_id'],))
                        c.execute('DELETE FROM catfood_standard_brand_candidate WHERE raw_brand_name=%s', (tag,))
                        for owned_task_id in task_ids:
                            for table in ('pipeline_node_output','pipeline_task_node','formula_calculation_task'):
                                c.execute(f'DELETE FROM {table} WHERE task_id=%s', (owned_task_id,))
                            c.execute('DELETE FROM pipeline_task WHERE id=%s', (owned_task_id,))
                conn.commit()


def test_real_scores_reuse_promote_recalculate_and_failure_isolation(calculation_case, monkeypatch):
    from main import app
    from services import formula_calculation_service as calc, orchestrator_service as jobs
    from services import catfood_standardization_service as standard
    from services.orchestrator_worker import _execute_task
    from services import market_profile_rebuild_service as market
    sid, task_id = calculation_case
    t = time.monotonic();_execute_task(app, task_id)
    task = jobs.get_task(task_id)
    assert task['task_status'] == 'done', task.get('error_message')
    run = calc.task_run(task_id);fid = run['formula_id']
    first = calc.published([fid])[fid]
    assert first['result_stage'] == 'provisional' and len(first['portrait']['dimensions']) == 6 and len(first['risks']) == 2
    print('private formula full computation seconds:', round(time.monotonic()-t, 2))
    assert calc.mark_market_dirty(fid)['queued']
    with calc.connect() as conn:
        with conn.cursor() as c:
            c.execute('SELECT status,is_current FROM catfood_standard_formula WHERE formula_id=%s', (fid,))
            assert c.fetchone() == {'status': 'draft', 'is_current': 0}
            c.execute('SELECT * FROM formula_market_dirty WHERE formula_id=%s', (fid,))
            assert not c.fetchone()
    jobs.reset_node_for_reextract(task_id, 'brand_standardize')
    t = time.monotonic();_execute_task(app, task_id)
    assert jobs.get_task(task_id)['task_status'] == 'done'
    assert calc.published([fid])[fid]['result_id'] == first['result_id']
    assert len([n for n in jobs.get_task(task_id)['nodes'] if n['node_status'] == 'skipped']) == 5
    print('cache path seconds:', round(time.monotonic()-t, 2))
    with calc.formula_lock(fid) as one:
        with calc.formula_lock(fid) as two:
            assert one and not two
    # Simulate a confirmation without changing identity, then real promotion code.
    with calc.connect() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE catfood_standard_formula SET confirmation_status='confirmed' WHERE formula_id=%s", (fid,))
        conn.commit()
    standard.build_formula_feature_input(formula_id=fid)
    assert calc.promote(fid)
    promoted = calc.published([fid])[fid]
    assert promoted['result_id'] == first['result_id'] and promoted['result_stage'] == 'official'
    assert promoted['portrait'] == first['portrait'] and promoted['risks'] == first['risks']
    with calc.connect() as conn:
        with conn.cursor() as c:
            c.execute('UPDATE catfood_standard_formula SET nutrition_json=%s WHERE formula_id=%s', (json.dumps({'protein': {'metric_name':'粗蛋白','value':32,'unit':'%','operator':'>='}}), fid))
        conn.commit()
    jobs.reset_node_for_reextract(task_id, 'formula_input_build');_execute_task(app, task_id)
    assert jobs.get_task(task_id)['task_status'] == 'done'
    current = calc.published([fid])[fid]
    assert current['result_id'] != first['result_id'] and current['nutrition'] != first['nutrition']
    with calc.connect() as conn:
        with conn.cursor() as c:
            c.execute('SELECT result_json FROM formula_calculation_run WHERE id=%s', (first['result_id'],))
            assert calc.decoded(c.fetchone()['result_json'])['portrait'] == first['portrait']
            c.execute('INSERT INTO formula_market_dirty(formula_id) VALUES(%s)', (fid,))
        conn.commit()
    with patch.object(market, '_execute_steps', side_effect=RuntimeError('injected market failure')):
        with pytest.raises(RuntimeError, match='injected'):
            calc.drain_market_once()
    assert calc.published([fid])[fid]['result_id'] == current['result_id']
    with calc.connect() as conn:
        with conn.cursor() as c:
            c.execute('SELECT * FROM formula_market_dirty WHERE formula_id=%s', (fid,))
            assert c.fetchone(), 'failure must retain durable retry entry'
    # Genuine rule-version change cannot reuse the previous publication.
    monkeypatch.setenv('CHONGXI_NUTRITION_STANDARD_VERSION', 'qa38-revised')
    jobs.reset_node_for_reextract(task_id, 'formula_input_build');_execute_task(app, task_id)
    assert jobs.get_task(task_id)['task_status'] == 'done'
    assert calc.published([fid])[fid]['result_id'] != current['result_id']

    # A legacy baseline with no publication is lazily queued without rerunning OCR.
    with calc.connect() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE catfood_standard_formula SET status='active',is_current=1 WHERE formula_id=%s", (fid,))
            c.execute('UPDATE catfood_standard_product SET active=1 WHERE product_id=(SELECT product_id FROM catfood_standard_formula WHERE formula_id=%s)', (fid,))
            c.execute('UPDATE catfood_ocr_standard_mapping SET formula_id=%s WHERE source_id=%s', (fid,sid))
            c.execute('DELETE FROM formula_calculation_current WHERE formula_id=%s', (fid,))
        conn.commit()
    baseline_task = calc.queue_formula_refresh(fid)
    assert baseline_task and baseline_task != task_id
    assert calc.queue_formula_refresh(fid) == baseline_task
    _execute_task(app, baseline_task)
    assert jobs.get_task(baseline_task)['task_status'] == 'done'
    assert calc.published([fid])[fid]['portrait']
    # Two distinct uploads/work items for the same revision share one computation.
    from concurrent.futures import ThreadPoolExecutor
    for iteration in range(3):
        monkeypatch.setenv('CHONGXI_NUTRITION_STANDARD_VERSION', f'qa38-concurrent-{iteration}')
        for job_id in (task_id, baseline_task):
            jobs.reset_node_for_reextract(job_id, 'formula_input_build')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda job_id: _execute_task(app, job_id), (task_id, baseline_task)))
        # A lock contender deliberately leaves its ready node for the next worker sweep.
        for job_id in (task_id, baseline_task):
            if jobs.get_task(job_id)['task_status'] != 'done':
                _execute_task(app, job_id)
            current_task = jobs.get_task(job_id)
            if current_task['task_status'] != 'done':
                print('CONCURRENCY DIAGNOSTIC', json.dumps(current_task, default=str), flush=True)
            assert current_task['task_status'] == 'done', {'task_id': job_id, 'nodes': [{k:n.get(k) for k in ('node_code','node_status','error_message','claim_token')} for n in current_task['nodes']]}
        assert calc.task_run(task_id)['id'] == calc.task_run(baseline_task)['id']
        assert any(n['node_status'] == 'skipped' for job_id in (task_id, baseline_task) for n in jobs.get_task(job_id)['nodes'])
