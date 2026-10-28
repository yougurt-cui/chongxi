"""Measure an exact code snapshot on the disposable QA38 database.

Run through local-dev/qa38-benchmark/run_comparison.py. Never accepts a production
host/port. OCR is a fixed fixture; no score computation or market step is mocked.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import resource
import sys
import time
import uuid

parser = argparse.ArgumentParser()
parser.add_argument('--code-root', required=True)
parser.add_argument('--variant', choices=['baseline', 'candidate'], required=True)
parser.add_argument('--formula-id', type=int, required=True)
parser.add_argument('--scenario', choices=['known', 'unknown', 'prepare'], default='known')
parser.add_argument('--output', required=True)
args = parser.parse_args()
sys.path.insert(0, args.code_root)
os.chdir(args.code_root)
os.environ.update(MYSQL_HOST='127.0.0.1', MYSQL_PORT='13308', MYSQL_USER='root', MYSQL_PASSWORD='',
                  MYSQL_DATABASE='csv_labeling', CHONGXI_PIPELINE_WORKER='0',
                  CHONGXI_VERSIONED_CALCULATION='1' if args.variant == 'candidate' else '0',
                  CHONGXI_NUTRITION_STANDARD_VERSION='benchmark-fixed-v1')
from app_config import get_mysql_config, get_feature_mysql_config
import pymysql
for cfg in (get_mysql_config(), get_feature_mysql_config()):
    assert cfg['host'] == '127.0.0.1' and int(cfg['port']) == 13308, 'disposable DB only'

def connection(feature=False):
    return pymysql.connect(**(get_feature_mysql_config() if feature else get_mysql_config()), cursorclass=pymysql.cursors.DictCursor)

with connection() as conn:
    with conn.cursor() as c:
        c.execute('SELECT @@port AS port');assert c.fetchone()['port'] == 13308

from main import app
from services import orchestrator_service as jobs
from services import miniprogram_food_submission_service as submissions
from services.catfood_standardization_service import init_standardization_db
from services.orchestrator_worker import _execute_task
from api import orchestrator_api

init_standardization_db();jobs.init_db();submissions.init_food_submission_tables()
if args.variant == 'candidate':
    from services import formula_calculation_service as calc
    calc.migrate()

with connection() as conn:
    with conn.cursor() as c:
        c.execute('''SELECT f.formula_id,p.standard_product_name,b.standard_brand_name,m.source_id
            FROM catfood_standard_formula f JOIN catfood_standard_product p ON p.product_id=f.product_id
            JOIN catfood_standard_brand b ON b.brand_id=p.brand_id
            JOIN catfood_ocr_standard_mapping m ON m.formula_id=f.formula_id
            WHERE f.formula_id=%s ORDER BY m.source_id LIMIT 1''', (args.formula_id,))
        identity = c.fetchone();assert identity
        c.execute('SELECT * FROM catfood_ingredient_ocr_parsed WHERE source_id=%s ORDER BY id DESC LIMIT 1', (identity['source_id'],))
        seed = c.fetchone();assert seed
        c.execute('SELECT * FROM product_guarantee WHERE source_id=%s', (identity['source_id'],))
        guarantees = c.fetchall()

# Same evidence, new source per upload. Known fixtures use the canonical submitted
# names; unknown identity changes only these names, not the ingredient evidence.
def create_submission(index):
    sid = 380000000000 + args.formula_id * 10 + index
    uid = 'qa38-benchmark-user'
    row = dict(seed);row.pop('id')
    brand = identity['standard_brand_name'] if args.scenario != 'unknown' else f'QA38未确认品牌{args.formula_id}'
    product = identity['standard_product_name'] if args.scenario != 'unknown' else f'QA38未确认配方{args.formula_id}'
    row.update(source_id=sid, brand=brand, product_name=product, image_group_key=f'qa38bench-{sid}',
               file_sha256=uuid.uuid4().hex*2, merged_source_ids=str(sid), image_name=f'qa38bench-{sid}.jpg', parse_batch_id='qa38-benchmark')
    with connection() as conn:
        with conn.cursor() as c:
            keys = list(row)
            c.execute('INSERT INTO catfood_ingredient_ocr_parsed ('+','.join('`'+k+'`' for k in keys)+') VALUES ('+','.join(['%s']*len(keys))+')', [row[k] for k in keys])
            parsed_id = c.lastrowid
            for original in guarantees:
                g = dict(original);g.pop('id');g.update(source_id=sid, parsed_row_id=parsed_id, formula_id=None,
                    file_sha256=row['file_sha256'], image_name=row['image_name'], extract_batch_id='qa38-benchmark')
                keys = list(g)
                c.execute('INSERT INTO product_guarantee ('+','.join('`'+k+'`' for k in keys)+') VALUES ('+','.join(['%s']*len(keys))+')', [g[k] for k in keys])
        conn.commit()
    task = jobs.create_task('catfood_image_analysis', {'brand_name': brand, 'product_name': product, 'awaiting_submission_commit': True})
    tid = task['id'];subid = uuid.uuid4().hex
    with connection() as conn:
        jobs._save_node_output(conn, tid, 'ocr_formula', {**row, 'parsed_row_id': parsed_id})
        for code in ('upload_check', 'ocr_formula'):
            jobs._set_node_status(conn, tid, code, jobs.NODE_SUCCESS, None)
        jobs._set_task_status(conn, tid, jobs.TASK_PROCESSING)
        with conn.cursor() as c:
            c.execute("UPDATE pipeline_task SET payload_json=JSON_REMOVE(payload_json,'$.awaiting_submission_commit') WHERE id=%s", (tid,))
            c.execute('''INSERT INTO miniprogram_food_submission(id,user_id,claimed_brand,claimed_product_name,orchestrator_task_id,
                recognition_status,status,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,'processing','active',UTC_TIMESTAMP(),UTC_TIMESTAMP())''', (subid, uid, brand, product, tid))
        conn.commit()
    jobs._refresh_ready_nodes(jobs.get_task(tid))
    return tid, subid, uid


def run_once(index, state):
    tid, subid, uid = create_submission(index)
    events = [];first = None;probes = [];original_api = orchestrator_api._call_api_sync;original_apply = jobs.apply_node_result
    def timed_api(api, payload):
        t = time.perf_counter();answer = original_api(api, payload)
        events.append({'node': payload.get('orchestrator_node_code') or api['url'], 'seconds': time.perf_counter()-t,
                       'ok': answer['ok'], 'error': (answer.get('data') or {}).get('error'),
                       'market_steps': (answer.get('data') or {}).get('steps') if 'market-profile' in api['url'] else None})
        return answer
    def applied(*a, **kw):
        nonlocal first
        result = original_apply(*a, **kw)
        if first is None and a[1] in {'product_standardize','formula_input_build','product_portrait_summary','formula_profile','market_profile_build'}:
            t = time.perf_counter()
            with app.app_context(), app.test_request_context():
                item = submissions.get_food_submission(uid, subid)['item']
            probes.append(time.perf_counter()-t)
            if item.get('result_available'):
                first = time.perf_counter()-started
        return result
    orchestrator_api._call_api_sync = timed_api;jobs.apply_node_result = applied
    cpu = time.process_time();child = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    try:
        _execute_task(app, tid)
        with app.app_context(), app.test_request_context():
            item = submissions.get_food_submission(uid, subid)['item']
        if first is None and item.get('result_available'):first = time.perf_counter()-started
        elapsed = time.perf_counter()-started
        child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_elapsed = time.process_time()-cpu+child_after.ru_utime+child_after.ru_stime-child.ru_utime-child.ru_stime
    finally:
        orchestrator_api._call_api_sync = original_api;jobs.apply_node_result = original_apply
    task = jobs.get_task(tid)
    fid = item.get('formula_id')
    dims = None;risks = None;raw_scores = None;quality_flags = None
    if item.get('result_available') and fid:
        from services.guardian_product_portrait_service import load_product_portraits
        from services.guardian_food_analysis_service import load_risks
        with app.app_context(), app.test_request_context():
            portrait = load_product_portraits([fid]).get(int(fid)) or {}
            dims = {key: value.get('score') for key,value in (portrait.get('dimensions') or {}).items()}
            risks = load_risks([fid]).get(int(fid))
        with connection(True) as conn:
            with conn.cursor() as c:
                c.execute('''SELECT w.protein_quality_score AS protein_quality,
                    s.protein_score AS protein,s.fat_score AS fat,s.fiber_score AS fiber,
                    s.carb_score AS carbohydrate,s.prebiotic_score AS prebiotic
                    FROM sku_feature_input s LEFT JOIN catfood_protein_fat_fiber_score_wide w
                    ON w.formula_id=s.formula_id WHERE s.formula_id=%s AND s.feature_version='soft_v1'
                    ORDER BY s.id DESC LIMIT 1''', (fid,))
                raw_scores = c.fetchone()
        if args.variant == 'candidate':
            with app.app_context(), app.test_request_context():
                quality_flags = calc.published([fid]).get(int(fid), {}).get('quality_flags')
    return {'variant': args.variant, 'scenario': args.scenario, 'seed_formula_id': args.formula_id, 'state': state,
        'first_available_seconds': first, 'completion_seconds': elapsed, 'result_available': bool(item.get('result_available')),
        'result_stage': item.get('result_stage'), 'task_status': task['task_status'], 'error': task.get('error_message'),
        'cpu_seconds': cpu_elapsed,
        'probe_overhead_seconds': sum(probes), 'node_timings': events,
        'node_statuses': {n['node_code']:n['node_status'] for n in task['nodes']}, 'scores': dims, 'risks': risks,
        'raw_scores': raw_scores, 'quality_flags': quality_flags}

# Remove only the readable publication for this identity. Existing working tables
# remain, matching a real recalc/new-publication path and preserving the reference
# pool values. Both versions must run scores before a valid portrait exists.
with connection(True) as conn:
    with conn.cursor() as c:c.execute('DELETE FROM catfood_formula_portrait_summary WHERE formula_id=%s', (args.formula_id,))
    conn.commit()
with connection() as conn:
    with conn.cursor() as c:
        for table in ('formula_calculation_task',):
            c.execute(f'DELETE t FROM {table} t JOIN formula_calculation_run r ON r.id=t.run_id WHERE r.formula_id=%s', (args.formula_id,))
        for table in ('formula_calculation_current','formula_calculation_run'):
            c.execute(f'DELETE FROM {table} WHERE formula_id=%s', (args.formula_id,))
    conn.commit()
results = [run_once(1, 'no_valid_publication')]
if args.scenario == 'known' and results[0]['result_available']:
    results.append(run_once(2, 'repeated_upload'))
if args.variant == 'candidate' and args.scenario == 'known':
    # Measure the deferred cost separately; do not present it as eliminated work.
    t = time.perf_counter()
    background = calc.drain_market_once()
    results[0]['background_market'] = {'seconds': time.perf_counter()-t, **background}
Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str))
for r in results:
    print(json.dumps({k:r[k] for k in ('variant','scenario','seed_formula_id','state','first_available_seconds','completion_seconds','task_status','error')},ensure_ascii=False),flush=True)
