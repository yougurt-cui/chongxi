"""Versioned, immutable calculation publications over the existing score pipeline.

Enable after running scripts/migrate_formula_calculations.py. The old score
surfaces remain working tables; customers read one published bundle per request.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

import pymysql
from app_config import get_mysql_config, get_feature_mysql_config


def enabled():
    return os.getenv('CHONGXI_VERSIONED_CALCULATION', '0') == '1'


def connect():
    return pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def decoded(value, default=None):
    return json.loads(value) if isinstance(value, str) else value if value is not None else default


def input_fingerprint(row):
    # Provisional ingredient proxies may leave the visible composition unchanged,
    # but they must never reuse or promote an official calculation publication.
    value = {
        'normalized_ingredient_composition': row.get('normalized_ingredient_composition'),
        'normalized_ingredients_json': decoded(row.get('normalized_ingredients_json'), {}),
        'nutrition_json': decoded(row.get('nutrition_json'), {}),
        'standard_brand_name': row.get('standard_brand_name'),
        'result_stage': row.get('result_stage') or 'official',
        'quality_flags_json': decoded(row.get('quality_flags_json'), []),
    }
    return digest(value)


@lru_cache(maxsize=1)
def algorithm_fingerprint():
    root = Path(__file__).resolve().parents[1]
    paths = list((root / 'vendor/feature_score_pipeline').rglob('*.py'))
    paths += [root / 'scripts/run_formula_phase.py',
              root / 'scripts/backfill_formula_ingredient_features.py',
              root / 'scripts/rebuild_protein_source_from_profiles.py',
              root / 'services/ingredient_coarse_proxy_service.py',
              root / 'services/product_portrait_summary_service.py',
              root / 'services/formula_incremental_service.py',
              root / 'services/catfood_standardization_service.py',
              root / 'services/formula_calculation_service.py']
    return digest([(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(paths)])


def dependency_components():
    """Conservative first release: include all editable scoring knowledge.

    Changes invalidate results even when an administrator edits SQL directly.
    This intentionally favours correctness over fine-grained cache reuse.
    """
    tables = ('catfood_standard_ingredient', 'catfood_standard_ingredient_alias',
              'catfood_ingredient_feature_rule', 'catfood_feature_label',
              'catfood_ingredient_science_profile', 'catfood_science_score_mapping',
              'catfood_score_weight_config', 'catfood_score_position_weight_config',
              'catfood_score_range_config', 'catfood_score_enum_config', 'catfood_label_score_config',
              'catfood_score_threshold_config', 'catfood_ingredient_noise_pool')
    values = {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SHOW TABLES')
            available = {next(iter(r.values())) for r in cur.fetchall()}
            for table in tables:
                if table in available:
                    cur.execute(f'SELECT * FROM `{table}`')
                    rows = [{k: v for k, v in row.items() if k not in {'created_at', 'updated_at'}} for row in cur.fetchall()]
                    values[table] = sorted(rows, key=dumps)
                else:
                    values[table] = None
    scoring = digest({'algorithm': algorithm_fingerprint(), 'knowledge': values,
                      'nutrition_standard': os.getenv('CHONGXI_NUTRITION_STANDARD_VERSION', 'current-v1')})
    values = {}
    with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
        with conn.cursor() as cur:
            for table in ('reference_sku_pool_snapshot', 'brand_symptom_stats', 'brand_soft_stool_stats'):
                cur.execute(f'SELECT * FROM `{table}`')
                values[table] = sorted(({k: v for k, v in r.items() if k not in {'created_at', 'updated_at'}}
                                        for r in cur.fetchall()), key=dumps)
            # The existing risk models join the reference membership to live features.
            # Hash only members and mathematical inputs, never each new upload/batch.
            cur.execute("""SELECT DISTINCT f.formula_id,f.sku_id,f.feature_version,f.brand_name,
                f.protein_score,f.carb_score,f.fiber_score,COALESCE(ff.fat_score,f.fat_score) AS fat_score,
                f.prebiotic_score,f.antioxidant_score,ff.fat_regulation_score,ff.omega_imbalance_score,
                fiber.p_form_score,fiber.p_bulk_score,COALESCE(f.p_buffer,fiber.p_buffer_score) AS p_buffer,
                COALESCE(f.q_feed,fiber.q_feed_score) AS q_feed,COALESCE(f.q_scfa,fiber.q_scfa_score) AS q_scfa
                FROM reference_sku_pool_snapshot p JOIN sku_feature_input f
                ON p.feature_version=f.feature_version AND
                  ((p.formula_id IS NOT NULL AND p.formula_id=f.formula_id) OR (p.formula_id IS NULL AND p.sku_id=f.sku_id))
                LEFT JOIN catfood_fat_material_features_scored ff ON f.formula_id=ff.formula_id OR
                  (f.formula_id IS NULL AND f.sku_id COLLATE utf8mb4_unicode_ci=ff.product_key COLLATE utf8mb4_unicode_ci)
                LEFT JOIN catfood_fiber_feature_score fiber ON f.formula_id=fiber.formula_id OR
                  (f.formula_id IS NULL AND f.sku_id=fiber.product_key)""")
            values['reference_features'] = sorted(cur.fetchall(), key=dumps)
    return {'scoring': scoring, 'risk_reference': digest(values)}


def dependency_fingerprint():
    return digest(dependency_components())


def migrate():
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS formula_calculation_run (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY, formula_id BIGINT UNSIGNED NOT NULL,
                calculation_key CHAR(64) NOT NULL UNIQUE, input_fingerprint CHAR(64) NOT NULL,
                dependency_fingerprint CHAR(64) NOT NULL, input_json JSON NOT NULL,
                status VARCHAR(24) NOT NULL, result_stage VARCHAR(24) NOT NULL,
                result_json JSON NULL, error_message TEXT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_calculation_formula(formula_id,id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            cur.execute('''CREATE TABLE IF NOT EXISTS formula_calculation_current (
                formula_id BIGINT UNSIGNED PRIMARY KEY, requested_run_id BIGINT UNSIGNED NOT NULL,
                published_run_id BIGINT UNSIGNED NULL, invalidated TINYINT NOT NULL DEFAULT 0
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            cur.execute('''CREATE TABLE IF NOT EXISTS formula_calculation_task (
                task_id VARCHAR(64) PRIMARY KEY, run_id BIGINT UNSIGNED NOT NULL,
                KEY idx_calculation_task_run(run_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            cur.execute('''CREATE TABLE IF NOT EXISTS formula_calculation_draft (
                source_id BIGINT PRIMARY KEY, formula_id BIGINT UNSIGNED NOT NULL UNIQUE,
                product_id BIGINT UNSIGNED NOT NULL, brand_id BIGINT NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
            cur.execute('''CREATE TABLE IF NOT EXISTS formula_market_dirty (
                formula_id BIGINT UNSIGNED PRIMARY KEY, generation BIGINT NOT NULL DEFAULT 1,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')
        conn.commit()


@contextmanager
def formula_lock(formula_id):
    with connect() as conn:
        name = f'formula-calculation:{int(formula_id)}'
        with conn.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s,0) AS acquired', (name,))
            acquired = bool(cur.fetchone()['acquired'])
        try:
            yield acquired
        finally:
            if acquired:
                with conn.cursor() as cur:
                    cur.execute('SELECT RELEASE_LOCK(%s)', (name,))


def read_input(formula_id):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM catfood_formula_feature_input WHERE formula_id=%s', (formula_id,))
            row = cur.fetchone()
    if not row:
        raise ValueError('配方计算输入不存在')
    return row


def begin(task_id, formula_id, *, _input=None, _dependencies=None, _expected_run_id=None):
    row = dict(_input) if _input is not None else read_input(formula_id)
    parts = _dependencies if _dependencies is not None else dependency_components()
    row['_dependency_components'] = parts
    fingerprint, dependencies = input_fingerprint(row), digest(parts)
    key = digest([int(formula_id), fingerprint, dependencies])
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT task_status FROM pipeline_task WHERE id=%s FOR UPDATE', (task_id,))
            task = cur.fetchone()
            if not task or task['task_status'] == 'cancelled':
                raise ValueError('投稿已撤销或任务不存在，不开始计算')
            if _expected_run_id is not None:
                # Rebinding risk dependencies is allowed only for the same input
                # and still-current execution. Never relabel old scores as new rules.
                cur.execute('''SELECT t.run_id,c.requested_run_id FROM formula_calculation_task t
                    JOIN formula_calculation_current c ON c.formula_id=%s
                    WHERE t.task_id=%s FOR UPDATE''', (formula_id, task_id))
                current = cur.fetchone() or {}
                if current.get('run_id') != _expected_run_id or current.get('requested_run_id') != _expected_run_id:
                    raise CalculationSuperseded('计算版本已被替代')
                cur.execute('SELECT * FROM catfood_formula_feature_input WHERE formula_id=%s FOR UPDATE', (formula_id,))
                if input_fingerprint(cur.fetchone() or {}) != fingerprint:
                    raise CalculationSuperseded('计算输入已更新')
            cur.execute('''INSERT INTO formula_calculation_run
                (formula_id,calculation_key,input_fingerprint,dependency_fingerprint,input_json,status,result_stage)
                VALUES(%s,%s,%s,%s,%s,'pending',%s)
                ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)''',
                (formula_id, key, fingerprint, dependencies, dumps(row), row.get('result_stage') or 'provisional'))
            run_id = cur.lastrowid
            cur.execute('SELECT * FROM formula_calculation_run WHERE id=%s FOR UPDATE', (run_id,))
            run = cur.fetchone()
            cur.execute('''INSERT INTO formula_calculation_current(formula_id,requested_run_id)
                VALUES(%s,%s) ON DUPLICATE KEY UPDATE requested_run_id=VALUES(requested_run_id)''', (formula_id, run_id))
            cur.execute('''INSERT INTO formula_calculation_task(task_id,run_id) VALUES(%s,%s)
                ON DUPLICATE KEY UPDATE run_id=VALUES(run_id)''', (task_id, run_id))
            if _expected_run_id is not None and int(run_id) != _expected_run_id:
                cur.execute("UPDATE formula_calculation_run SET status='superseded' WHERE id=%s AND status<>'published'", (_expected_run_id,))
            if run['status'] == 'published':
                if row.get('result_stage') == 'official':
                    cur.execute("UPDATE formula_calculation_run SET result_stage='official' WHERE id=%s", (run_id,))
                    run['result_stage'] = 'official'
                cur.execute('UPDATE formula_calculation_current SET published_run_id=%s,invalidated=0 WHERE formula_id=%s', (run_id, formula_id))
            else:
                cur.execute("UPDATE formula_calculation_run SET status='running',error_message=NULL WHERE id=%s", (run_id,))
                cur.execute('UPDATE formula_calculation_current SET invalidated=1 WHERE formula_id=%s', (formula_id,))
        conn.commit()
    return {'calculation_run_id': int(run_id), 'calculation_key': key,
            'reused_calculation': run['status'] == 'published', 'result_stage': run['result_stage']}


def task_run(task_id):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT r.* FROM formula_calculation_task t JOIN formula_calculation_run r ON r.id=t.run_id
                WHERE t.task_id=%s''', (task_id,))
            return cur.fetchone()


def capture_bundle(formula_id, batch_id, run):
    """Read working rows only while the worker owns the formula lock."""
    from services.product_portrait_summary_service import build_portrait, _load_feature_rows, validated_feature_row
    with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
        with conn.cursor() as cur:
            rows = _load_feature_rows(cur, 'soft_v1', [formula_id], include_drafts=True)
            if not rows:
                raise ValueError('缺少本次计算的产品画像')
            features = validated_feature_row(rows[0])
            portrait = build_portrait(features)
            cur.execute("SELECT batch_id FROM sku_feature_input WHERE formula_id=%s AND feature_version='soft_v1'", (formula_id,))
            if (cur.fetchone() or {}).get('batch_id') != batch_id:
                raise ValueError('评分与当前计算批次不一致')
            cur.execute('''SELECT score_model_version,reference_pool_version,current_pool_risk_level,main_reason_tags,
                support_reason_tags,fat_detail_tags
                FROM sku_risk_score_result WHERE formula_id=%s AND batch_id=%s''', (formula_id, batch_id))
            risk_rows = cur.fetchall()
    risks = []
    for row in risk_rows:
        model = row['score_model_version']
        kind = 'soft_stool' if model.startswith('SOFT_STOOL') else 'black_chin' if model.startswith('BLACK_CHIN') else None
        if kind:
            risks.append({'kind': kind, 'model_version': model, 'reference_pool_version': row['reference_pool_version'],
                          'relative_risk_level': row['current_pool_risk_level'],
                          'reason_tags': reason_tags(row['main_reason_tags']),
                          'support_reason_tags': reason_tags(row.get('support_reason_tags')),
                          'fat_detail_tags': reason_tags(row.get('fat_detail_tags')),
                          'note': '相对于模型参考池的结构风险，不是患病概率或病因。'})
    if {r['kind'] for r in risks} != {'soft_stool', 'black_chin'}:
        raise ValueError('本次计算的风险结果尚未完整生成')
    portrait['calculation_version'] = run['dependency_fingerprint']
    flags = list(decoded(decoded(run['input_json'], {}).get('quality_flags_json'), []))
    flags.extend('invalid_score:' + field for field in
                 ('protein_quality_score', 'protein_score', 'fat_score', 'fiber_score', 'carb_score', 'prebiotic_score')
                 if rows[0].get(field) is not None and features[field] is None)
    return {'portrait': portrait, 'nutrition': snapshot_nutrition(decoded(run['input_json'], {})),
            'features': {**features, 'source_feature_id': rows[0]['id']}, 'risks': risks,
            'quality_flags': flags,
            'input_fingerprint': run['input_fingerprint'], 'calculation_version': run['dependency_fingerprint']}


def reason_tags(value):
    try:
        tags = decoded(value, [])
    except ValueError:
        tags = [value] if value else []
    return [str(tag) for tag in tags[:5]] if isinstance(tags, list) else []


class CalculationSuperseded(ValueError):
    pass


def prepare_risks(task_id):
    """Bind risk inputs after scores settle, without repeating unaffected maths.

    A reference-pool member may update its own live feature row during scoring.
    Only reference/brand-feedback changes can rebind here. Input or scoring-rule
    changes still require a full restart; publication rechecks all dependencies.
    """
    run = task_run(task_id)
    if not run:
        raise ValueError('缺少本次计算版本')
    snapshot = decoded(run['input_json'], {})
    current = dependency_components()
    if digest(current) == run['dependency_fingerprint']:
        return {'risk_dependencies_refreshed': False}
    previous = snapshot.get('_dependency_components') or {}
    if not previous.get('scoring') or previous['scoring'] != current['scoring']:
        raise CalculationSuperseded('基础评分规则更新，重新计算后再生成风险结果')
    result = begin(task_id, int(run['formula_id']), _input=snapshot,
                   _dependencies=current, _expected_run_id=int(run['id']))
    return {**result, 'risk_dependencies_refreshed': True}


def publish(task_id, batch_id, *, claim_token=None):
    run = task_run(task_id)
    if not run:
        raise ValueError('缺少本次计算版本')
    formula_id = int(run['formula_id'])
    if (input_fingerprint(read_input(formula_id)) != run['input_fingerprint']
            or dependency_fingerprint() != run['dependency_fingerprint']):
        raise CalculationSuperseded('计算期间输入或规则已更新，请重新计算；旧结果未被覆盖')
    bundle = capture_bundle(formula_id, batch_id, run)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT task_status FROM pipeline_task WHERE id=%s FOR UPDATE', (task_id,))
            task = cur.fetchone()
            if not task or task['task_status'] == 'cancelled':
                raise ValueError('投稿已撤销，不发布结果')
            cur.execute("SELECT node_status,claim_token FROM pipeline_task_node WHERE task_id=%s AND node_code='formula_profile' FOR UPDATE", (task_id,))
            node = cur.fetchone() or {}
            if node.get('node_status') != 'running' or node.get('claim_token') != claim_token:
                raise ValueError('执行已被重置，旧任务不发布结果')
            cur.execute('SELECT * FROM catfood_formula_feature_input WHERE formula_id=%s FOR UPDATE', (formula_id,))
            if input_fingerprint(cur.fetchone() or {}) != run['input_fingerprint']:
                raise CalculationSuperseded('输入已经更新，旧任务不发布结果')
            cur.execute('SELECT requested_run_id FROM formula_calculation_current WHERE formula_id=%s FOR UPDATE', (formula_id,))
            if int(cur.fetchone()['requested_run_id']) != int(run['id']):
                raise CalculationSuperseded('计算已被较新的输入版本替代')
            cur.execute("UPDATE formula_calculation_run SET status='published',result_json=%s WHERE id=%s AND status<>'published'", (dumps(bundle), run['id']))
            cur.execute('UPDATE formula_calculation_current SET published_run_id=%s,invalidated=0 WHERE formula_id=%s', (run['id'], formula_id))
        conn.commit()
    return {'result_id': str(run['id']), 'result_stage': run['result_stage']}


def published(formula_ids):
    ids = sorted({int(v) for v in formula_ids if v})
    if not enabled() or not ids:
        return {}
    # Pin each formula to one publication for the whole Flask request.
    from flask import has_request_context, g
    cache = g.setdefault('_formula_publications', {}) if has_request_context() else {}
    missing = [v for v in ids if v not in cache]
    if missing:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f'''SELECT c.formula_id,c.invalidated,c.requested_run_id,c.published_run_id,
                    r.result_stage,r.result_json,r.updated_at,q.id AS input_run_id,q.input_json,q.dependency_fingerprint FROM formula_calculation_current c
                    LEFT JOIN formula_calculation_run r ON r.id=c.published_run_id
                    JOIN formula_calculation_run q ON q.id=c.requested_run_id
                    WHERE c.formula_id IN ({','.join(['%s'] * len(missing))})''', missing)
                rows = cur.fetchall()
        cache.update({v: None for v in missing})
        for row in rows:
            bundle = decoded(row['result_json'], {}) if not row['invalidated'] else {}
            if not bundle.get('portrait') and row.get('input_json'):
                snapshot = decoded(row['input_json'], {})
                bundle = {'nutrition': snapshot_nutrition(snapshot), 'quality_flags': decoded(snapshot.get('quality_flags_json'), []),
                          'partial': True, 'missing_dimensions': ['配方画像', '风险结果'],
                          'calculation_version': row['dependency_fingerprint']}
            if row['result_stage'] == 'official':
                bundle['quality_flags'] = [f for f in bundle.get('quality_flags', []) if f not in {'formula_unconfirmed', 'identity_unconfirmed'}]
            cache[int(row['formula_id'])] = {**bundle, 'result_id': str(row['published_run_id']) if row['published_run_id'] else None,
                'result_stage': row['result_stage'], 'updating': row['requested_run_id'] != row['published_run_id'],
                'invalidated': bool(row['invalidated']), 'generated_at': str(row['updated_at'] or '')}
    return {v: cache[v] for v in ids if cache.get(v) is not None}


def promote(formula_id):
    row = read_input(formula_id)
    if row.get('result_stage') != 'official':
        return False
    fp, dep = input_fingerprint(row), dependency_fingerprint()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''UPDATE formula_calculation_run r JOIN formula_calculation_current c ON c.published_run_id=r.id
                SET r.result_stage='official' WHERE c.formula_id=%s AND c.invalidated=0
                AND c.requested_run_id=c.published_run_id
                AND r.input_fingerprint=%s AND r.dependency_fingerprint=%s AND r.status='published' ''', (formula_id, fp, dep))
            changed = cur.rowcount
        conn.commit()
    from flask import has_request_context, g
    if has_request_context():
        g.pop('_formula_publications', None)
    return bool(changed) or any(v.get('result_stage') == 'official' and v.get('input_fingerprint') == fp
                               and v.get('calculation_version') == dep for v in published([formula_id]).values())


def mark_market_dirty(formula_id):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT f.status,i.result_stage FROM catfood_standard_formula f
                LEFT JOIN catfood_formula_feature_input i ON i.formula_id=f.formula_id
                WHERE f.formula_id=%s""", (formula_id,))
            formula = cur.fetchone()
            queued = bool(formula and formula['status'] == 'active' and formula.get('result_stage') == 'official')
            if queued:
                cur.execute('''INSERT INTO formula_market_dirty(formula_id) VALUES(%s)
                    ON DUPLICATE KEY UPDATE generation=generation+1,updated_at=UTC_TIMESTAMP()''', (formula_id,))
        conn.commit()
    return {'ok': True, 'formula_id': formula_id, 'scope': 'deferred_market', 'queued': queued,
            'reason': None if queued else 'provisional_result_not_published_to_market'}


def draft_identity(source_id):
    """An isolated calculation identity; never a product alias or public SKU."""
    from services import catfood_standardization_service as standard
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM catfood_ocr_standard_mapping WHERE source_id=%s FOR UPDATE', (source_id,))
            mapping = cur.fetchone()
            if not mapping:
                raise ValueError('缺少上传原始数据')
            # Recheck after locking the source: two tasks may share one OCR source.
            cur.execute('SELECT * FROM formula_calculation_draft WHERE source_id=%s', (source_id,))
            existing = cur.fetchone()
            normalized, ingredients, fingerprint = standard._normalize_ingredients_with_noise(cur, mapping.get('raw_ingredient_composition'))
            if not ingredients:
                raise ValueError('缺少可计算的原料内容')
            nutrition = standard._load_nutrition_signature(cur, source_id)
            if existing:
                cur.execute('''UPDATE catfood_standard_formula SET raw_ingredient_example=%s,
                    normalized_ingredient_composition=%s,normalized_ingredients_json=%s,
                    ingredient_fingerprint=%s,nutrition_json=%s WHERE formula_id=%s AND status='draft' ''',
                    (mapping.get('raw_ingredient_composition') or normalized, normalized, dumps(ingredients),
                     fingerprint, dumps(nutrition), existing['formula_id']))
                conn.commit()
                return existing
            brand_id = -(10**12 + int(source_id))
            cur.execute('''INSERT INTO catfood_standard_brand(brand_id,standard_brand_name,active)
                VALUES(%s,%s,0)''', (brand_id, f'__calculation_{source_id}'))
            name = f'__calculation_{source_id}'
            cur.execute('''INSERT INTO catfood_standard_product(brand_id,standard_product_name,display_name,active,candidate_type)
                VALUES(%s,%s,%s,0,'calculation_draft')''', (brand_id, name, (mapping.get('raw_product_name') or '待确认产品')[:255]))
            product_id = cur.lastrowid
            cur.execute('''INSERT INTO catfood_standard_formula(product_id,formula_version,raw_ingredient_example,
                normalized_ingredient_composition,normalized_ingredients_json,ingredient_fingerprint,nutrition_json,
                is_current,status,confirmation_status) VALUES(%s,1,%s,%s,%s,%s,%s,0,'draft','provisional')''',
                (product_id, mapping.get('raw_ingredient_composition') or normalized, normalized, dumps(ingredients), fingerprint, dumps(nutrition)))
            formula_id = cur.lastrowid
            cur.execute('INSERT INTO formula_calculation_draft(source_id,formula_id,product_id,brand_id) VALUES(%s,%s,%s,%s)',
                        (source_id, formula_id, product_id, brand_id))
        conn.commit()
    return {'source_id': source_id, 'formula_id': formula_id, 'product_id': product_id, 'brand_id': brand_id}


def allow_upload_preview(result, payload, node):
    """Only uploads may continue with a private identity; admin review stays intact."""
    status_field = node + '_status'
    if not enabled() or not payload.get('orchestrator_task_id') or result.get(status_field) in {'matched', 'provisional'}:
        return result
    if result.get(status_field) not in {'pending', 'blocked', 'conflict'} or not result.get('source_id'):
        return result
    try:
        draft = draft_identity(int(result['source_id']))
    except ValueError:
        return result  # Missing ingredient evidence remains blocked, never fabricated.
    return {**result, **draft, status_field: 'provisional', 'identity_review_status': result[status_field],
            'private_calculation': True, 'quality_flags': ['identity_unconfirmed']}


def drain_market_once():
    """Dedicated low-priority CLI worker; coalesce dirty rows into one rebuild."""
    from services.market_profile_rebuild_service import _connect, LOCK_NAME, _execute_steps
    with _connect(autocommit=True) as lock:
        with lock.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s,0) AS acquired', (LOCK_NAME,))
            if not cur.fetchone()['acquired']:
                return {'status': 'busy'}
        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT formula_id,generation FROM formula_market_dirty')
                    rows = list(cur.fetchall())
            if not rows:
                return {'status': 'idle'}
            steps = _execute_steps()
            with connect() as conn:
                with conn.cursor() as cur:
                    for row in rows:
                        cur.execute('DELETE FROM formula_market_dirty WHERE formula_id=%s AND generation=%s',
                                    (row['formula_id'], row['generation']))
                conn.commit()
            return {'status': 'success', 'formulas': len(rows), 'steps': steps}
        finally:
            with lock.cursor() as cur:
                cur.execute('SELECT RELEASE_LOCK(%s)', (LOCK_NAME,))


def requeue_sources(source_ids, node='brand_standardize'):
    if not enabled() or not source_ids:
        return []
    from services import pipeline_recalculation_service as requests
    ids = sorted({int(v) for v in source_ids})
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f'''SELECT DISTINCT t.id FROM pipeline_task t JOIN pipeline_node_output o ON o.task_id=t.id
                WHERE t.task_status<>'cancelled' AND o.node_code='ocr_formula'
                AND CAST(JSON_UNQUOTE(JSON_EXTRACT(o.output_json,'$.source_id')) AS UNSIGNED)
                IN ({','.join(['%s']*len(ids))})''', ids)
            tasks = [r['id'] for r in cur.fetchall()]
    return requests.request_many(tasks, node, '审核信息更新，等待当前流程结束及正式门禁通过')


def requeue_formulas(formula_ids, *, invalidate=False):
    if not enabled() or not formula_ids:
        return []
    from services import pipeline_recalculation_service as requests
    ids = sorted({int(v) for v in formula_ids})
    marks = ','.join(['%s']*len(ids))
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f'''SELECT DISTINCT t.id FROM pipeline_task t
                JOIN formula_calculation_task b
                  ON CONVERT(b.task_id USING utf8mb4) COLLATE utf8mb4_unicode_ci
                   = CONVERT(t.id USING utf8mb4) COLLATE utf8mb4_unicode_ci
                JOIN formula_calculation_run r ON r.id=b.run_id
                WHERE t.task_status<>'cancelled' AND r.formula_id IN ({marks})
                UNION SELECT DISTINCT t.id FROM pipeline_task t
                JOIN pipeline_node_output o ON o.task_id=t.id
                WHERE t.task_status<>'cancelled' AND o.node_code IN ('formula_standardize','formula_input_build')
                AND CAST(JSON_UNQUOTE(JSON_EXTRACT(o.output_json,'$.formula_id')) AS UNSIGNED) IN ({marks})''', ids + ids)
            tasks = [r['id'] for r in cur.fetchall()]
    return requests.request_many(tasks, 'formula_input_build', '原料或计算规则更新，等待正式重算',
                                 invalidate_formula_ids=ids if invalidate else ())


_last_dependency_scan = 0.0


def reconcile_dependencies():
    """Conservative invalidation on actual policy changes, not on every upload."""
    global _last_dependency_scan
    if not enabled() or time.monotonic() - _last_dependency_scan < 60:
        return
    _last_dependency_scan = time.monotonic()
    dep = dependency_fingerprint()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT c.formula_id FROM formula_calculation_current c
                JOIN formula_calculation_run r ON r.id=c.requested_run_id
                WHERE r.dependency_fingerprint<>%s AND c.invalidated=0
                  AND r.status='published' LIMIT 20''', (dep,))
            ids = [r['formula_id'] for r in cur.fetchall()]
    requeue_formulas(ids, invalidate=True)


def submission_reply(submission, current_formula_id=None):
    from services.guardian_product_portrait_service import load_product_portraits, compare_portraits, portrait_comparison_text
    bundle = submission['calculation_result']
    target = bundle['portrait']
    provisional = bundle.get('result_stage') == 'provisional'
    name = ' '.join(filter(None, [submission.get('recognized_brand') or submission.get('claimed_brand'),
                                 submission.get('recognized_product_name') or submission.get('claimed_product_name')]))
    lines = [f"{name}：{'初算结果（待核对）' if provisional else '正式结果'}", target.get('summary') or '已有可用的配方数据。']
    if provisional:
        lines.append('基于当前上传资料计算，核对后可能更新；暂不据此认定商品身份。')
    current = load_product_portraits([current_formula_id]).get(int(current_formula_id)) if current_formula_id else None
    comparison = compare_portraits(current, target)
    if current_formula_id and comparison['status'] == 'unavailable':
        if queue_formula_refresh(current_formula_id):
            comparison['note'] = '当前口粮的同版本画像正在更新，完成后再次分析即可比较；本次先展示上传粮的结果。'
    lines.append(portrait_comparison_text(comparison) if current_formula_id else '尚未记录当前口粮，补充后可比较共同特点与差异。')
    return {'response_type': 'result_card', 'reply': '\n\n'.join(lines),
            'result': {'intent': 'food_analysis', 'status': 'ready', 'result_id': bundle['result_id'],
                       'result_stage': bundle['result_stage'], 'portrait_comparison': comparison,
                       'product_portrait': target, 'comparison_current_portrait': current,
                       'quality_flags': bundle.get('quality_flags') or [], 'food_candidates': []}}


def snapshot_nutrition(input_row):
    from services.guardian_food_analysis_service import number, NUTRIENTS
    nutrition = decoded(input_row.get('nutrition_json'), {})
    result = {}
    for item in nutrition.values():
        if not isinstance(item, dict) or item.get('metric_name') not in NUTRIENTS:
            continue
        value = number(item.get('value'))
        unit = str(item.get('unit') or '').replace('％', '%').strip().lower() or None
        if value is None or value < 0 or (unit == '%' and value > 100):
            continue
        name = item['metric_name']
        record = {'name': name, 'value': value, 'unit': unit, 'basis': item.get('basis'),
                  'operator': {'≥': '>=', '≤': '<='}.get(item.get('operator'), item.get('operator')),
                  'source': 'calculation_input_snapshot'}
        if name in result and result[name] != record:
            result[name] = {'name': name, 'value': None, 'status': 'conflicting'}
        else:
            result[name] = record
    return result


def queue_formula_refresh(formula_id):
    """Lazily upgrade a legacy comparison baseline using the same durable queue.

    No OCR or network calls are replayed. The caller only receives a job ID;
    score work is owned by the normal bounded worker.
    """
    if not enabled() or not formula_id:
        return None
    from services import orchestrator_service as jobs
    from services.catfood_standardization_service import _canonical_formula_source
    with connect() as conn:
        with conn.cursor() as cur:
            name = f'formula-queue:{int(formula_id)}'
            cur.execute('SELECT GET_LOCK(%s,0) AS acquired', (name,))
            if not cur.fetchone()['acquired']:
                return None
            try:
                cur.execute("""SELECT id,task_status FROM pipeline_task
                    WHERE JSON_EXTRACT(payload_json,'$.calculation_only_formula_id')=%s
                    AND task_status<>'cancelled' ORDER BY created_at DESC LIMIT 1""", (int(formula_id),))
                previous = cur.fetchone()
                if previous and previous['task_status'] not in {'done', 'failed', 'need_review'}:
                    return previous['id']
                if previous and previous['task_status'] in {'failed', 'need_review'}:
                    return previous['id']  # Human-visible failure; no unbounded automatic retry.
                if previous:
                    jobs.reset_node_for_reextract(previous['id'], 'formula_input_build', '升级比较基线')
                    return previous['id']
                cur.execute("""SELECT f.*,p.brand_id FROM catfood_standard_formula f
                    JOIN catfood_standard_product p ON p.product_id=f.product_id
                    WHERE f.formula_id=%s AND f.status='active' AND f.is_current=1 AND p.active=1""", (formula_id,))
                formula = cur.fetchone()
                if not formula:
                    return None
                source, _ = _canonical_formula_source(cur, formula_id=int(formula_id))
                if not source:
                    return None
                task = jobs.create_task('catfood_image_analysis', {
                    'calculation_only_formula_id': int(formula_id), 'awaiting_submission_commit': True})
                task_id = task['id']
                identity = {'source_id': source['source_id'], 'formula_id': int(formula_id),
                            'product_id': formula['product_id'], 'brand_id': formula['brand_id'],
                            'formula_status': 'matched', 'product_status': 'matched', 'brand_status': 'matched'}
                with jobs._connect() as write:
                    for code in ('upload_check', 'ocr_formula', 'brand_standardize', 'formula_precheck',
                                 'product_standardize', 'formula_standardize'):
                        jobs._save_node_output(write, task_id, code, {**source, **identity})
                        jobs._set_node_status(write, task_id, code, jobs.NODE_SUCCESS, None)
                    jobs._set_node_status(write, task_id, 'formula_input_build', jobs.NODE_READY, None)
                    jobs._set_task_status(write, task_id, jobs.TASK_PROCESSING, None)
                    with write.cursor() as w:
                        w.execute("UPDATE pipeline_task SET payload_json=JSON_REMOVE(payload_json,'$.awaiting_submission_commit') WHERE id=%s", (task_id,))
                    write.commit()
                jobs.wake_dispatcher()
                return task_id
            finally:
                cur.execute('SELECT RELEASE_LOCK(%s)', (name,))
