"""Behavior parsing state lives on the original collection batch."""
import hashlib
from services import model_knowledge_import_service as store
PARSER_VERSION = 'behavior-observation-v2'


def parse_lock_name(batch_id):
    return 'behavior-parse:' + hashlib.sha256((store.database_name()+':'+str(batch_id)).encode()).hexdigest()[:40]


def ensure_schema(cur):
    cur.execute("SHOW COLUMNS FROM knowledge_import_batch LIKE 'behavior_parse_result'")
    if not cur.fetchone():cur.execute('ALTER TABLE knowledge_import_batch ADD COLUMN behavior_parse_result JSON NULL')
    from services.behavior_knowledge_schema import require_aligned
    require_aligned(cur)
    from services.behavior_knowledge_summary_service import DDL
    cur.execute(DDL)


def snapshot_call(cur, batch_id, request_prompt=None):
    cur.execute("SELECT * FROM knowledge_import_batch WHERE id=%s AND knowledge_base='behavior'",(batch_id,))
    batch=cur.fetchone()
    if not batch:raise ValueError('行为采集批次不存在')
    state=store.decode(batch.get('behavior_parse_result')) or {}
    current=hashlib.sha256((batch.get('raw_response') or '').encode()).hexdigest()
    archived=state.get('collection_hash') or (state.get('legacy_call') or {}).get('response_hash')
    if archived and archived!=current:raise ValueError('已归档行为回答不允许覆盖，请新建采集任务')
    state['collection_hash']=current
    if request_prompt is not None:state['request_prompt']=request_prompt
    _save(cur,batch_id,state)
    return batch_id


def ensure_call(batch_id):
    with store.connect() as conn, conn.cursor() as cur:
        result=snapshot_call(cur,batch_id);conn.commit();return result


def parse_batch(batch_id):
    store.ensure_database()
    with store.connect() as conn, conn.cursor() as cur:
        lock=parse_lock_name(batch_id)
        cur.execute('SELECT GET_LOCK(%s,0) acquired',(lock,))
        if not cur.fetchone()['acquired']:return {'batch_id':batch_id,'status':'running'}
        state={}
        try:
            cur.execute("SELECT * FROM knowledge_import_batch WHERE id=%s AND knowledge_base='behavior'",(batch_id,));batch=cur.fetchone()
            if not batch or batch['status'] not in ('success','parse_failed'):raise ValueError('只能解析成功的行为采集批次')
            state=store.decode(batch.get('behavior_parse_result')) or {}
            state.update(parse_status='running',parse_attempts=state.get('parse_attempts',0)+1,
                         parser_version=PARSER_VERSION,parse_error=None,
                         parse_source_hash=hashlib.sha256((batch.get('raw_response') or '').encode()).hexdigest())
            _save(cur,batch_id,state);conn.commit()
            try:
                from vendor.chongxi_multi_model_mysql.run import extract_json
                from services.behavior_knowledge_service import _import_points
                doc=extract_json(batch['raw_response'] or '',knowledge_base='behavior',question=batch['question'])
                result=_import_points(batch_id,doc,source_hash=state['parse_source_hash'],parser_version=PARSER_VERSION)
                state.update(parse_status='completed',point_count=result['total_count'],parse_error=None)
            except Exception as exc:
                state.update(parse_status='failed',parse_error=str(exc)[:2000]);raise
            finally:
                from datetime import datetime,timezone
                state['parsed_at']=datetime.now(timezone.utc).isoformat()
                state.setdefault('parse_history',[]).append({k:v for k,v in state.items() if k not in ('parse_history','legacy_call')})
                _save(cur,batch_id,state);conn.commit()
            from services.behavior_knowledge_summary_service import refresh
            try:
                summary=refresh()
                state.update(summary_status='completed',summary_error=None)
            except Exception as exc:
                state.update(summary_status='failed',summary_error=str(exc)[:2000])
                raise
            finally:
                _save(cur,batch_id,state);conn.commit()
            return {**result,'parse_attempt':state['parse_attempts'],'summary':summary}
        finally:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))


def _save(cur,batch_id,state):
    cur.execute('UPDATE knowledge_import_batch SET behavior_parse_result=%s WHERE id=%s',(store.dumps(state),batch_id))


def task_status(task_id):
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM knowledge_import_batch WHERE task_id=%s AND knowledge_base='behavior' ORDER BY id",(task_id,));rows=list(cur.fetchall())
    calls=[]
    for row in rows:
        state=store.decode(row.get('behavior_parse_result')) or {}
        public_state={k:v for k,v in state.items() if k not in ('legacy_call','parse_history','request_prompt')}
        calls.append({**row,**public_state,'batch_id':row['id'],'source_call_id':None,
                      'call_status':'api_failed' if row['status']=='api_failed' else 'success',
                      'call_error':row.get('error_message'),'parse_error':state.get('parse_error') or state.get('summary_error')})
    return {'calls':calls,'collection_table':'knowledge_import_batch','knowledge_table':'bbehavior_knowledge_point_detail',
            'summary_table':'behavior_knowledge_point','parse_log_table':'knowledge_import_batch.behavior_parse_result'}
