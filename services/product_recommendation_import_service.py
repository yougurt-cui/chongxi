"""Read product recommendation source objects; write a single candidate table with recommendation provenance."""
import os
from pathlib import Path
import re
import json
import pymysql
from app_config import get_mysql_config
from services import model_knowledge_import_service as source_store
from vendor.pet_product_import.import_products import normalize, digest

SCHEMA = Path(__file__).resolve().parents[1] / 'vendor/pet_product_import/schema.sql'


def target_database():
    name = os.getenv('PRODUCT_KNOWLEDGE_DATABASE', 'chongxi_product')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', name) or name.lower() in {
        source_store.database_name().lower(), 'csv_labeling', 'protein_feature_platform',
        str(get_mysql_config()['database']).lower()}:
        raise ValueError('产品候选库必须独立于原始知识库和正式产品库')
    return name


def target_connect():
    return pymysql.connect(**get_mysql_config(database=target_database()),
                           cursorclass=pymysql.cursors.DictCursor, autocommit=False)


def ensure_tables():
    name = target_database()
    config = get_mysql_config(database=name); config.pop('database')
    with pymysql.connect(**config, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    with target_connect() as conn, conn.cursor() as cur:
        # Execute only the CREATE TABLE statement; never USE a hard-coded database.
        sql = '\n'.join(line for line in SCHEMA.read_text().splitlines() if not line.lstrip().startswith('--'))
        for statement in sql.split(';'):
            if statement.strip().startswith('CREATE TABLE'):
                cur.execute(statement)
        cur.execute('SHOW COLUMNS FROM product_recommendation')
        required = {'recommendation_key', 'input_hash', 'product_key', 'breed_code',
                    'life_stage_code', 'need_code', 'sources', 'raw_json'}
        if not required <= {r['Field'] for r in cur.fetchall()}:
            raise ValueError('检测到旧表结构，请先运行 scripts/merge_product_recommendations.py --apply 迁移；不自动覆盖旧表')
        conn.commit()


def read_sources(*, source_id=None, after_id=0, limit=None):
    clauses, params = ['category=%s', 'id>%s'], ['product_recommendation', after_id]
    if source_id is not None:
        clauses.append('id=%s'); params.append(source_id)
    sql = 'SELECT * FROM knowledge_point_source WHERE ' + ' AND '.join(clauses) + ' ORDER BY id'
    if limit is not None:
        sql += ' LIMIT %s'; params.append(limit)
    with source_store.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


GROUP_FIELDS = ('species_code', 'breed_code', 'life_stage_code', 'need_code')


def group_records(records):
    """One row per species/breed/stage/need/brand/product; retain every source."""
    grouped = {}
    seen = {}
    for record in sorted(records, key=lambda row: row['source_key']):
        source_key = record['source_key']
        if source_key in seen and seen[source_key] != record:
            raise ValueError('同一推荐来源出现冲突内容')
        seen[source_key] = record
        for recommendation in record['recommendations']:
            identity = [recommendation[k] for k in GROUP_FIELDS] + [record['product_key']]
            key = digest(identity)
            group = grouped.setdefault(key, {
                'recommendation_key': key, 'product_key': record['product_key'],
                **{k: recommendation[k] for k in GROUP_FIELDS},
                'brand': record['brand'], 'product_name': record['product_name'],
                '_records': {}, '_recommendations': {}})
            group['_records'][source_key] = record
            group['_recommendations'][digest([source_key, recommendation])] = (source_key, recommendation)
    result = []
    for key in sorted(grouped):
        group = grouped[key]
        originals = group.pop('_records')
        recommendations = group.pop('_recommendations')
        sources = []
        reasons, descriptions, relations, models = set(), set(), set(), set()
        for signature in sorted(recommendations):
            source_key, recommendation = recommendations[signature]
            record = originals[source_key]
            source = record['sources'][0]
            ctx = source.get('source_context') or {}
            if isinstance(ctx, str): ctx = json.loads(ctx)
            sources.append({'source_key': source_key, 'knowledge_source_id': record['knowledge_source_id'],
                'original_source_id': record['original_source_id'], 'batch_id': source.get('batch_id'),
                'source_model': source.get('source_model'), 'source_provider': source.get('source_provider'),
                'source_question': ctx.get('question'), 'source_evidence_status': ctx.get('evidence_status'),
                'recommendation': recommendation, 'original_source': source})
            if recommendation.get('recommendation_reason'): reasons.add(recommendation['recommendation_reason'])
            if record.get('product_description'): descriptions.add(record['product_description'])
            relations.add(recommendation['relation_type'])
            models.add((source.get('source_provider'), source.get('source_model')))
        group.update(product_description='\n\n'.join(sorted(descriptions)),
                     recommendation_reason='\n\n'.join(sorted(reasons)), relation_types=sorted(relations),
                     source_count=len(originals), model_count=len(models), sources=sources,
                     raw_json={'records': [originals[k] for k in sorted(originals)]})
        result.append(group)
    return result


def stored_records(rows):
    records = {}
    for row in rows:
        raw = row['raw_json']
        if isinstance(raw, str): raw = json.loads(raw)
        for record in raw['records']:
            key = record['source_key']
            if key in records and records[key] != record:
                raise ValueError('已有合并记录的来源内容不一致，请先核对')
            records[key] = record
    return records


def write_record(conn, record, table='product_recommendation'):
    """Write one six-dimension group, including its full provenance array."""
    if not re.fullmatch(r'product_recommendation(?:_stage_[0-9a-f]{12})?', table):
        raise ValueError('无效产品推荐表名')
    fingerprint = digest(record)
    with conn.cursor() as cur:
        cur.execute(f'SELECT id,input_hash,review_status,verification_status FROM {table} WHERE recommendation_key=%s FOR UPDATE',
                    (record['recommendation_key'],))
        previous = cur.fetchone()
        if previous and previous['input_hash'] == fingerprint:
            return previous['id'], 'unchanged'
        if previous and (previous['review_status'] != 'pending' or previous['verification_status'] != 'pending'):
            raise ValueError('合并内容发生变化且存在人工审核记录，请人工处理后重试')
        columns = ('recommendation_key', 'product_key', *GROUP_FIELDS, 'brand', 'product_name',
                   'product_description', 'recommendation_reason', 'relation_types', 'source_count',
                   'model_count', 'sources', 'raw_json', 'input_hash')
        json_columns = {'relation_types', 'sources', 'raw_json'}
        values = [fingerprint if k == 'input_hash' else
                  json.dumps(record[k], ensure_ascii=False) if k in json_columns else record[k] for k in columns]
        if previous:
            cur.execute(f'UPDATE {table} SET ' +
                        ','.join(f'{col}=%s' for col in columns) + ' WHERE id=%s', (*values, previous['id']))
            row_id = previous['id']
        else:
            cur.execute(f'INSERT INTO {table} (' + ','.join(columns) + ') VALUES (' +
                        ','.join(['%s'] * len(columns)) + ')', values)
            row_id = cur.lastrowid
    return row_id, 'imported'


def lock_name():
    return 'product-import:' + digest(target_database())[:40]


def import_records(records):
    ensure_tables()
    counts = {'imported': 0, 'unchanged': 0, 'removed_groups': 0}
    with target_connect() as conn:
        acquired = False
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT GET_LOCK(%s,10) acquired', (lock_name(),))
                acquired = bool(cur.fetchone()['acquired'])
                if not acquired: raise ValueError('产品推荐导入正在运行，请稍后重试')
                cur.execute('SELECT * FROM product_recommendation FOR UPDATE')
                existing = list(cur.fetchall())
            all_records = stored_records(existing)
            incoming = {}
            for record in records:
                key = record['source_key']
                if key in incoming and incoming[key] != record:
                    raise ValueError('同一推荐来源出现冲突内容')
                incoming[key] = record
            all_records.update(incoming)
            groups = group_records(all_records.values())
            keys = {g['recommendation_key'] for g in groups}
            for group in groups:
                _, status = write_record(conn, group)
                counts[status] += 1
            with conn.cursor() as cur:
                for row in existing:
                    if row['recommendation_key'] in keys: continue
                    if row['review_status'] != 'pending' or row['verification_status'] != 'pending':
                        raise ValueError('需要移除的旧分组存在人工审核，请人工处理后重试')
                    cur.execute('DELETE FROM product_recommendation WHERE id=%s', (row['id'],))
                    counts['removed_groups'] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if acquired:
                with conn.cursor() as cur: cur.execute('SELECT RELEASE_LOCK(%s)', (lock_name(),))
    return counts
