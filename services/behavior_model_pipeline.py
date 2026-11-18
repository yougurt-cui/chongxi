"""Independent raw model-call snapshots and explicit, repeatable behavior parsing."""
import hashlib
from services import model_knowledge_import_service as store

PARSER_VERSION = 'behavior-observation-v2'
CALL_DDL = '''CREATE TABLE IF NOT EXISTS behavior_model_call (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 batch_id BIGINT UNSIGNED NOT NULL,
 task_id BIGINT UNSIGNED NOT NULL,
 provider VARCHAR(32) NOT NULL,
 model VARCHAR(255) NOT NULL,
 question TEXT NOT NULL,
 prompt_template LONGTEXT NOT NULL,
 request_prompt LONGTEXT NULL,
 web_search BOOLEAN NOT NULL,
 raw_response LONGTEXT NULL,
 response_hash CHAR(64) NOT NULL,
 search_metadata JSON NULL,
 call_status ENUM('success','api_failed') NOT NULL,
 call_error TEXT NULL,
 prompt_tokens BIGINT NULL,
 completion_tokens BIGINT NULL,
 latency_ms BIGINT NULL,
 created_at DATETIME NOT NULL,
 finished_at DATETIME NULL,
 UNIQUE KEY uk_behavior_call_batch (batch_id), KEY idx_behavior_call_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''
PARSE_DDL = '''CREATE TABLE IF NOT EXISTS behavior_knowledge_parse_run (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 source_call_id BIGINT UNSIGNED NOT NULL,
 source_hash CHAR(64) NOT NULL,
 parser_version VARCHAR(64) NOT NULL,
 status ENUM('running','completed','failed') NOT NULL,
 attempts INT NOT NULL DEFAULT 1,
 point_count INT NOT NULL DEFAULT 0,
 error_message TEXT NULL,
 started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 finished_at DATETIME NULL,
 UNIQUE KEY uk_behavior_parse (source_call_id,source_hash,parser_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''


def ensure_schema(cur):
    cur.execute(CALL_DDL); cur.execute(PARSE_DDL)
    cur.execute('SHOW COLUMNS FROM behavior_knowledge_point_detail')
    columns = {r['Field'] for r in cur.fetchall()}
    additions = {'source_call_id': 'BIGINT UNSIGNED NULL',
                 'source_hash': 'CHAR(64) NULL', 'parser_version': 'VARCHAR(64) NULL'}
    for name, path, length in [('breed', '$.breed', 120), ('life_stage', '$.life_stage', 120),
                              ('observation_code', '$.observation.observation_code', 160),
                              ('observation_name', '$.observation.observation_name', 160)]:
        additions[name] = f"VARCHAR({length}) GENERATED ALWAYS AS (CASE WHEN JSON_TYPE(JSON_EXTRACT(point_data,'{path}'))='STRING' THEN JSON_UNQUOTE(JSON_EXTRACT(point_data,'{path}')) ELSE NULL END) STORED"
    for name in ('normal_scenarios', 'abnormal_scenarios', 'unmapped_health_issues'):
        path = '$.observation.' + name
        additions[name] = f"JSON GENERATED ALWAYS AS (CASE WHEN JSON_TYPE(JSON_EXTRACT(point_data,'{path}'))='ARRAY' THEN JSON_EXTRACT(point_data,'{path}') ELSE NULL END) STORED"
    for name, definition in additions.items():
        if name not in columns:
            cur.execute('ALTER TABLE behavior_knowledge_point_detail ADD COLUMN `' + name + '` ' + definition)


def snapshot_call(cur, batch_id, request_prompt=None):
    """Record a completed API attempt, without parsing its text or touching knowledge."""
    cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s FOR UPDATE', (batch_id,))
    batch = cur.fetchone()
    if not batch or batch['knowledge_base'] != 'behavior':
        raise ValueError('行为采集批次不存在')
    if batch['status'] == 'running':
        raise ValueError('模型尚未返回结果')
    digest = hashlib.sha256((batch.get('raw_response') or '').encode()).hexdigest()
    cur.execute('SELECT * FROM behavior_model_call WHERE batch_id=%s', (batch_id,))
    existing = cur.fetchone()
    if existing:
        if existing['response_hash'] != digest or existing['prompt_template'] != batch['prompt_template']:
            raise ValueError('已归档调用内容与批次不一致，请人工核查，原始记录不覆盖')
        return existing['id']
    columns = ('batch_id','task_id','provider','model','question','prompt_template','request_prompt',
               'web_search','raw_response','response_hash','search_metadata','call_status','call_error',
               'prompt_tokens','completion_tokens','latency_ms','created_at','finished_at')
    failed = batch['status'] == 'api_failed'
    values = {**batch, 'batch_id': batch_id, 'request_prompt': request_prompt,
              'response_hash': digest, 'call_status': 'api_failed' if failed else 'success',
              'call_error': batch.get('error_message') if failed else None}
    cur.execute('INSERT INTO behavior_model_call (' + ','.join(columns) + ') VALUES (' +
                ','.join(['%s'] * len(columns)) + ')', tuple(values.get(k) for k in columns))
    return cur.lastrowid


def ensure_call(batch_id):
    with store.connect() as conn, conn.cursor() as cur:
        call_id = snapshot_call(cur, batch_id)
        conn.commit()
    return call_id


def parse_batch(batch_id):
    """Explicit stage: read the saved call only; never invoke a model or edit its log."""
    store.ensure_database()
    call_id = ensure_call(batch_id)
    with store.connect() as conn, conn.cursor() as cur:
        lock = 'behavior-parse:' + store.database_name() + ':' + str(call_id)
        lock = 'behavior-parse:' + hashlib.sha256(lock.encode()).hexdigest()[:40]
        cur.execute('SELECT GET_LOCK(%s,0) acquired', (lock,))
        if not cur.fetchone()['acquired']:
            return {'batch_id': batch_id, 'source_call_id': call_id, 'status': 'running'}
        try:
            cur.execute('SELECT * FROM behavior_model_call WHERE id=%s', (call_id,))
            call = cur.fetchone()
            if call['call_status'] != 'success':
                raise ValueError('模型调用失败，无法解析；需创建新的采集任务')
            cur.execute('''INSERT INTO behavior_knowledge_parse_run
                (source_call_id,source_hash,parser_version,status) VALUES (%s,%s,%s,'running')
                ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id),status='running',attempts=attempts+1,
                error_message=NULL,started_at=NOW(),finished_at=NULL''',
                (call_id, call['response_hash'], PARSER_VERSION))
            run_id = cur.lastrowid; conn.commit()
            try:
                from vendor.chongxi_multi_model_mysql.run import extract_json
                from services.behavior_knowledge_service import _import_points
                document = extract_json(call['raw_response'] or '', knowledge_base='behavior', question=call['question'])
                result = _import_points(batch_id, document, call_id=call_id,
                                        source_hash=call['response_hash'], parser_version=PARSER_VERSION)
            except Exception as exc:
                cur.execute("UPDATE behavior_knowledge_parse_run SET status='failed',error_message=%s,finished_at=NOW() WHERE id=%s",
                            (str(exc)[:2000], run_id)); conn.commit()
                raise
            cur.execute("UPDATE behavior_knowledge_parse_run SET status='completed',point_count=%s,finished_at=NOW() WHERE id=%s",
                        (result['total_count'], run_id)); conn.commit()
            return {**result, 'source_call_id': call_id, 'parse_run_id': run_id}
        finally:
            cur.execute('SELECT RELEASE_LOCK(%s)', (lock,))


def task_status(task_id):
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT c.id source_call_id,c.batch_id,c.provider,c.model,c.call_status,
            c.prompt_tokens,c.completion_tokens,c.latency_ms,c.call_error,c.raw_response,c.search_metadata,
            COALESCE(p.status,'pending') parse_status,p.error_message parse_error,p.point_count,
            p.parser_version,p.finished_at parsed_at
            FROM behavior_model_call c LEFT JOIN behavior_knowledge_parse_run p
              ON p.source_call_id=c.id AND p.source_hash=c.response_hash AND p.parser_version=%s
            WHERE c.task_id=%s ORDER BY c.batch_id''', (PARSER_VERSION, task_id))
        calls = list(cur.fetchall())
    return {'calls': calls, 'collection_table': 'behavior_model_call',
            'knowledge_table': 'behavior_knowledge_point_detail', 'parse_log_table': 'behavior_knowledge_parse_run'}
