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
PARSE_FIELDS = {
    'parse_status': "ENUM('pending','running','completed','failed') NOT NULL DEFAULT 'pending'",
    'parse_source_hash': 'CHAR(64) NULL', 'parser_version': 'VARCHAR(64) NULL',
    'parse_attempts': 'INT NOT NULL DEFAULT 0', 'point_count': 'INT NOT NULL DEFAULT 0',
    'parse_error': 'TEXT NULL', 'parse_started_at': 'DATETIME NULL', 'parsed_at': 'DATETIME NULL',
    'parse_history': 'JSON NULL',
}


def parse_lock_name(call_id):
    value = 'behavior-parse:' + store.database_name() + ':' + str(call_id)
    return 'behavior-parse:' + hashlib.sha256(value.encode()).hexdigest()[:40]


def ensure_schema(cur):
    cur.execute(CALL_DDL)
    cur.execute('SHOW COLUMNS FROM behavior_model_call')
    existing = {r['Field'] for r in cur.fetchall()}
    for name, definition in PARSE_FIELDS.items():
        if name not in existing:
            cur.execute('ALTER TABLE behavior_model_call ADD COLUMN `' + name + '` ' + definition)
    from services.behavior_knowledge_schema import require_aligned
    require_aligned(cur)


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
    """Explicit stage: parse saved text and update parsing metadata only."""
    store.ensure_database()
    call_id = ensure_call(batch_id)
    with store.connect() as conn, conn.cursor() as cur:
        lock = parse_lock_name(call_id)
        cur.execute('SELECT GET_LOCK(%s,0) acquired', (lock,))
        if not cur.fetchone()['acquired']:
            return {'batch_id': batch_id, 'source_call_id': call_id, 'status': 'running'}
        try:
            cur.execute('SELECT * FROM behavior_model_call WHERE id=%s', (call_id,))
            call = cur.fetchone()
            if call['call_status'] != 'success':
                raise ValueError('模型调用失败，无法解析；需创建新的采集任务')
            attempt = call.get('parse_attempts', 0) + 1
            cur.execute("""UPDATE behavior_model_call SET parse_status='running',parse_source_hash=%s,
                parser_version=%s,parse_attempts=%s,parse_error=NULL,parse_started_at=NOW(),parsed_at=NULL
                WHERE id=%s""", (call['response_hash'], PARSER_VERSION, attempt, call_id))
            conn.commit()
            try:
                from vendor.chongxi_multi_model_mysql.run import extract_json
                from services.behavior_knowledge_service import _import_points
                document = extract_json(call['raw_response'] or '', knowledge_base='behavior', question=call['question'])
                result = _import_points(batch_id, document, call_id=call_id,
                                        source_hash=call['response_hash'], parser_version=PARSER_VERSION)
            except Exception as exc:
                _finish_parse(cur, call_id, call['response_hash'], attempt, 'failed',
                              call.get('point_count', 0), str(exc)[:2000])
                conn.commit()
                raise
            _finish_parse(cur, call_id, call['response_hash'], attempt, 'completed', result['total_count'], None)
            conn.commit()
            return {**result, 'source_call_id': call_id, 'parse_attempt': attempt}
        finally:
            cur.execute('SELECT RELEASE_LOCK(%s)', (lock,))


def _finish_parse(cur, call_id, source_hash, attempt, status, count, error):
    cur.execute("""UPDATE behavior_model_call SET parse_status=%s,point_count=%s,parse_error=%s,
        parsed_at=NOW(),parse_history=JSON_ARRAY_APPEND(COALESCE(parse_history,JSON_ARRAY()),'$',
        JSON_OBJECT('source_hash',%s,'parser_version',%s,'attempt',%s,'status',%s,
                    'point_count',%s,'error_message',%s,'finished_at',NOW())) WHERE id=%s""",
        (status,count,error,source_hash,PARSER_VERSION,attempt,status,count,error,call_id))


def task_status(task_id):
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT id source_call_id,batch_id,provider,model,call_status,
            prompt_tokens,completion_tokens,latency_ms,call_error,raw_response,search_metadata,
            parse_status,parse_error,point_count,parser_version,parsed_at,parse_attempts
            FROM behavior_model_call WHERE task_id=%s ORDER BY batch_id''', (task_id,))
        calls = list(cur.fetchall())
    return {'calls': calls, 'collection_table': 'behavior_model_call',
            'knowledge_table': 'behavior_knowledge_point_detail', 'parse_log_table': 'behavior_model_call'}
