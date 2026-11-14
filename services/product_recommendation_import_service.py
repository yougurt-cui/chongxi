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
        required = {'source_key', 'input_hash', 'knowledge_source_id', 'product_key',
                    'recommendations', 'raw_json', 'source_model', 'source_provider'}
        if not required <= {r['Field'] for r in cur.fetchall()}:
            raise ValueError('检测到旧版多表结构，请选择新的 PRODUCT_KNOWLEDGE_DATABASE；不自动覆盖旧表')
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


def write_record(conn, record):
    fingerprint = digest(record)
    with conn.cursor() as cur:
        cur.execute('SELECT id,input_hash,review_status,verification_status FROM product_recommendation WHERE source_key=%s FOR UPDATE',
                    (record['source_key'],))
        previous = cur.fetchone()
        if previous and previous['input_hash'] == fingerprint:
            return previous['id'], 'unchanged'
        if previous and (previous['review_status'] != 'pending' or previous['verification_status'] != 'pending'):
            raise ValueError('来源已变更且存在人工审核记录，请人工处理后重试')
        source = record['sources'][0]
        ctx = source.get('source_context') or {}
        if isinstance(ctx, str):
            ctx = json.loads(ctx)
        columns = ('product_key', 'species_code', 'brand', 'product_name', 'product_description',
                   'source_key', 'input_hash', 'original_source_id', 'knowledge_source_id',
                   'batch_id', 'source_model', 'source_provider', 'source_question',
                   'source_evidence_status', 'recommendations', 'raw_json')
        values = [record[k] for k in columns[:5]] + [
            record['source_key'], fingerprint, record['original_source_id'], record['knowledge_source_id'],
            source.get('batch_id'), source.get('source_model'), source.get('source_provider'),
            ctx.get('question'), ctx.get('evidence_status'),
            json.dumps(record['recommendations'], ensure_ascii=False),
            json.dumps(record, ensure_ascii=False)]
        if previous:
            cur.execute('UPDATE product_recommendation SET ' +
                        ','.join(f'{col}=%s' for col in columns) + ' WHERE id=%s',
                        (*values, previous['id']))
            row_id = previous['id']
        else:
            cur.execute('INSERT INTO product_recommendation (' + ','.join(columns) + ') VALUES (' +
                        ','.join(['%s'] * len(columns)) + ')', values)
            row_id = cur.lastrowid
    return row_id, 'imported'


def import_records(records):
    ensure_tables()
    counts = {'imported': 0, 'unchanged': 0}
    with target_connect() as conn:
        try:
            for record in records:
                _, status = write_record(conn, record)
                counts[status] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return counts
