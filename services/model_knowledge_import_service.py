"""Two-table model collection store in an independent MySQL database."""
from __future__ import annotations
import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
import pymysql
from app_config import get_mysql_config
from vendor.chongxi_knowledge_importer import import_knowledge as parser

LOGGER=logging.getLogger(__name__)


def dumps(value):return json.dumps(value,ensure_ascii=False,sort_keys=True)


def database_name():
    name=os.getenv('MODEL_KNOWLEDGE_DATABASE') or 'chongxi_model_knowledge'
    forbidden={'csv_labeling','protein_feature_platform','chongxi_knowledge',str(get_mysql_config()['database']),
               os.getenv('MULTI_MODEL_MYSQL_DATABASE',''),os.getenv('KNOWLEDGE_DB','csv_labeling')}
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}',name) or name.lower() in {x.lower() for x in forbidden}:
        raise ValueError('模型知识库必须为独立数据库，不能指定业务库或旧知识库')
    return name


def connect():
    config=get_mysql_config(database=database_name())
    config.update(cursorclass=pymysql.cursors.DictCursor,autocommit=False,connect_timeout=10,read_timeout=30)
    return pymysql.connect(**config)


@lru_cache(maxsize=8)
def _ensure_database(name):
    config=get_mysql_config(database=name);config.pop('database',None)
    with pymysql.connect(**config,autocommit=True) as conn:
        with conn.cursor() as cur:cur.execute(f'CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
    with connect() as conn:
        with conn.cursor() as cur:
            for statement in (parser.ROOT/'schema.sql').read_text().split(';'):
                if statement.strip():cur.execute(statement)
            cur.execute("SHOW COLUMNS FROM knowledge_import_batch LIKE 'knowledge_base'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE knowledge_import_batch ADD COLUMN knowledge_base VARCHAR(32) NOT NULL DEFAULT 'selection' AFTER web_search")
                cur.execute('ALTER TABLE knowledge_import_batch ADD KEY idx_batch_knowledge_base (knowledge_base,created_at)')
            cur.execute("SHOW COLUMNS FROM knowledge_import_batch LIKE 'product_import_result'")
            if not cur.fetchone():
                try:
                    cur.execute('ALTER TABLE knowledge_import_batch ADD COLUMN product_import_result JSON NULL')
                except pymysql.err.OperationalError as exc:
                    if exc.args[0] != 1060: raise
            from services.knowledge_source_assembly_service import DDL
            cur.execute(DDL)
            cur.execute('SHOW COLUMNS FROM knowledge_point_source_detail')
            if not {'id','batch_id','source_provider','original_condition','species_code','breed_code','life_stage_codes','need_scene_code','need_scene_name','need_scene_codes','need_relation','scene_meta','need_codes','category_code','topic_code','content','standardization_meta'}.issubset({r['Field'] for r in cur.fetchall()}):
                raise RuntimeError('知识库字段尚未升级，请先执行对应字段扩展脚本')
        conn.commit()


def ensure_database():
    name=database_name();_ensure_database(name);return name


def decode(value):return json.loads(value) if isinstance(value,str) else value


def _triples(rows):
    from services.knowledge_standardization_service import source_value
    raw=[source_value(r) for r in rows]
    return {(tuple(sorted(r['applicable_conditions'])),r['topic'],r['content'],dumps(r.get('source_context'))) for r in raw}


def _import_batch_source(batch_id):
    target=ensure_database()
    conn=connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s FOR UPDATE',(batch_id,))
            batch=cur.fetchone()
            if not batch:raise LookupError('模型响应不存在')
            if batch['status']!='success':raise ValueError('只能解析成功的模型响应')
            if batch['knowledge_base'] == 'health':
                from vendor.chongxi_health_pipeline.task_parser import sources_from_dimensions
                sources = sources_from_dimensions(decode(batch['parsed_response']))
                rows = [{'content':s['content'],'topic':s['source_topic'],
                         'applicable_conditions':s['applicable_conditions']} for s in sources]
            else:
                _,rows=parser.to_rows(decode(batch['parsed_response']))
            cur.execute('SELECT * FROM knowledge_point_source_detail WHERE batch_id=%s',(batch_id,))
            existing=list(cur.fetchall())
            if batch['imported_at'] is not None and _triples(existing)==_triples(rows):
                conn.commit()
                return {'database':target,'batch_id':batch_id,'status':'already_imported','total_count':len(rows),'inserted_count':0}
            cur.execute('DELETE FROM knowledge_point_source_detail WHERE batch_id=%s',(batch_id,))
            from services.knowledge_standardization_service import pending_conditions, mapping_values, MAPPING_COLUMNS
            for row in rows:
                meta={'status':'pending','raw_input':{k:row[k] for k in ('applicable_conditions','topic')}}
                if row.get('source_context'):
                    meta['raw_input']['source_context']={**row['source_context'],'question':batch['question']}
                columns=','.join('`'+k+'`' for k in MAPPING_COLUMNS)
                cur.execute('INSERT INTO knowledge_point_source_detail (batch_id,source_provider,'+columns+',content,standardization_meta) VALUES ('+','.join(['%s']*11)+')',
                            (batch_id,batch['provider'])+mapping_values(pending_conditions(row)[0])+(row['content'],dumps(meta)))
            cur.execute('UPDATE knowledge_import_batch SET imported_at=NOW(),import_error=NULL WHERE id=%s',(batch_id,))
        conn.commit()
        from services.knowledge_source_assembly_service import refresh
        refresh()
        return {'database':target,'batch_id':batch_id,'status':'completed','total_count':len(rows),'inserted_count':len(rows)}
    except Exception:
        conn.rollback();raise
    finally:conn.close()



def import_batch(batch_id):
    ensure_database()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT knowledge_base FROM knowledge_import_batch WHERE id=%s',(batch_id,))
            batch=cur.fetchone()
            if not batch:raise LookupError('采集批次不存在')
            is_health = batch['knowledge_base'] == 'health'
    if not is_health:
        return _import_batch_source(batch_id)
    from services.health_knowledge_import_service import import_batch as import_health
    try:
        health=import_health(batch_id)
    except Exception:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE knowledge_import_batch SET import_error=%s WHERE id=%s',
                            ('健康知识结构化失败，原始数据已保留，可重试解析入库',batch_id))
            conn.commit()
        raise
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE knowledge_import_batch SET imported_at=COALESCE(imported_at,NOW()),import_error=NULL WHERE id=%s',(batch_id,))
        conn.commit()
    count=health['health_point_count']
    already=health['health_status']=='already_imported'
    return {'database':database_name(),'batch_id':batch_id,'status':'already_imported' if already else 'completed',
            'total_count':count,'inserted_count':0 if already else count,**health}


def _product_status(batch_id, result):
    with connect() as conn, conn.cursor() as cur:
        cur.execute('UPDATE knowledge_import_batch SET product_import_result=%s WHERE id=%s',
                    (dumps(result), batch_id))
        conn.commit()


def process_batch_pipeline(batch_id):
    """Shared automatic/manual path; product ingestion follows successful mapping and assembly."""
    ensure_database()
    with connect() as lock_conn, lock_conn.cursor() as lock_cur:
        from services.product_recommendation_import_service import digest
        lock = 'selection-pipeline:' + digest([database_name(), batch_id])[:40]
        lock_cur.execute('SELECT GET_LOCK(%s,0) acquired', (lock,))
        if not lock_cur.fetchone()['acquired']:
            return {'batch_id': batch_id, 'status': 'running'}
        selection = False
        try:
            lock_cur.execute('SELECT knowledge_base FROM knowledge_import_batch WHERE id=%s', (batch_id,))
            batch = lock_cur.fetchone()
            if not batch: raise LookupError('采集批次不存在')
            selection = batch['knowledge_base'] == 'selection'
            if selection: _product_status(batch_id, {'status': 'running', 'stage': 'standardization'})
            imported = import_batch(batch_id)
            if not selection: return imported
            from services.knowledge_standardization_service import normalize
            normalized = normalize(batch_id=batch_id)
            if (normalized.get('status') == 'paused' or any(normalized.get(k) for k in ('failed', 'busy', 'changed'))):
                raise ValueError('适用条件标准化尚未完成，请重试解析入库')
            _product_status(batch_id, {'status': 'running', 'stage': 'product_import'})
            from services.product_recommendation_import_service import import_batch_recommendations
            product = import_batch_recommendations(batch_id)
            _product_status(batch_id, product)
            with connect() as conn, conn.cursor() as cur:
                cur.execute('UPDATE knowledge_import_batch SET import_error=NULL WHERE id=%s', (batch_id,))
                conn.commit()
            return {**imported, 'product_import': product}
        except Exception as exc:
            if selection:
                message = str(exc) if isinstance(exc, ValueError) else '选粮解析或产品推荐入库失败，原始回答已保留，可重试'
                _product_status(batch_id, {'status': 'failed', 'message': message})
            raise
        finally:
            lock_cur.execute('SELECT RELEASE_LOCK(%s)', (lock,))


def persist_model_result(task_id,result):
    ensure_database()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM knowledge_import_batch WHERE task_id=%s AND provider=%s',(task_id,result['provider']))
            row=cur.fetchone()
            if not row:raise LookupError('采集任务不存在')
            batch_id=row['id']
            cur.execute('''UPDATE knowledge_import_batch SET status=%s,raw_response=%s,parsed_response=%s,
                search_metadata=%s,error_message=%s,prompt_tokens=%s,completion_tokens=%s,latency_ms=%s,
                finished_at=NOW() WHERE id=%s''',(result['status'],result['raw_text'],
                dumps(result['parsed']) if result['parsed'] is not None else None,
                dumps(result.get('search_metadata') or {}),result['error'],result.get('prompt_tokens'),
                result.get('completion_tokens'),result['latency_ms'],batch_id))
        conn.commit()
    if result['status']=='success':
        try:
            process_batch_pipeline(batch_id)
        except Exception:
            LOGGER.exception('model_response_import_failed batch_id=%s',batch_id)
            with connect() as conn:
                with conn.cursor() as cur:cur.execute('UPDATE knowledge_import_batch SET import_error=%s WHERE id=%s',('解析入库或标准化失败，原始回答已保存，可重试',batch_id))
                conn.commit()


def import_task(task_id):
    ensure_database()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,status FROM knowledge_import_batch WHERE task_id=%s ORDER BY id',(task_id,))
            rows=cur.fetchall()
    if not rows:raise LookupError('采集任务不存在')
    results=[];errors=[]
    for row in rows:
        if row['status']!='success':continue
        try:results.append(process_batch_pipeline(row['id']))
        except Exception:
            LOGGER.exception('model_response_reimport_failed batch_id=%s',row['id'])
            errors.append({'batch_id':row['id'],'message':'解析或入库失败，原始回答保留，请查看日志'})
    return {'database':database_name(),'task_id':task_id,'results':results,'errors':errors,
            'status':'partial_failed' if errors else 'running' if any(r.get('status') == 'running' for r in results) else 'completed' if results else 'no_successful_responses'}


def task_status(task_id):
    ensure_database()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT b.id,b.provider AS source_provider,b.status,b.import_error,b.knowledge_base,
                b.finished_at,CASE WHEN b.knowledge_base='health' THEN
                (SELECT COUNT(*) FROM health_knowledge_point_detail h WHERE h.batch_id=b.id) ELSE
                (SELECT COUNT(*) FROM knowledge_point_source_detail s WHERE s.batch_id=b.id) END AS total_count
                FROM knowledge_import_batch b WHERE b.task_id=%s AND b.imported_at IS NOT NULL ORDER BY b.id''',(task_id,))
            batches=list(cur.fetchall())
    return {'database':database_name(),'batches':batches}


def list_points(page=1,provider='',batch_id='',query='',knowledge_base=''):
    try:page=int(page)
    except (ValueError,TypeError):raise ValueError('页码不正确')
    if not 1<=page<=100000:raise ValueError('页码不正确')
    if len(query)>200:raise ValueError('搜索词不得超过200字')
    clauses=[];params=[]
    if provider:
        if provider not in ('qwen','deepseek','doubao','file'):raise ValueError('模型提供商不正确')
        clauses.append('b.provider=%s');params.append(provider)
    if knowledge_base:
        from services import multi_model_knowledge_service as tasks
        base_code,_=tasks.resolve_base(knowledge_base)
        clauses.append('b.knowledge_base=%s');params.append(base_code)
    if batch_id:
        try:batch_id=int(batch_id)
        except (ValueError,TypeError):raise ValueError('批次编号不正确')
        if batch_id<1:raise ValueError('批次编号不正确')
        clauses.append('s.batch_id=%s');params.append(batch_id)
    if query:clauses.append('(s.category_code LIKE %s OR s.topic_code LIKE %s OR s.content LIKE %s)');params.extend(['%'+query+'%']*3)
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    ensure_database()
    with connect() as conn:
        with conn.cursor() as cur:
            join=' FROM knowledge_point_source_detail s JOIN knowledge_import_batch b ON b.id=s.batch_id'
            cur.execute('SELECT COUNT(*) AS n'+join+where,params);total=cur.fetchone()['n']
            cur.execute('SELECT s.*,b.task_id,b.provider,b.model,b.knowledge_base'+join+where+' ORDER BY s.id DESC LIMIT 20 OFFSET %s',params+[(page-1)*20])
            items=list(cur.fetchall())
            cur.execute('SELECT COUNT(*) AS n FROM knowledge_import_batch');count=cur.fetchone()['n']
    for item in items:
        for field in ('life_stage_codes','need_codes','standardization_meta'):
            item[field]=decode(item.get(field))
    return {'database':database_name(),'items':items,'total':total,'page':page,'page_size':20,'batch_count':count}


def import_file(path,data):
    from services import multi_model_knowledge_service as tasks
    parser.to_rows(data)
    created=tasks.create_task({'question':data.get('question') or Path(path).name,
        'template':'文件导入 {{question}}','providers':['file']},launch=False,file_import=True)
    persist_model_result(created['task_id'],{'provider':'file','status':'success','raw_text':dumps(data),
        'parsed':data,'error':None,'latency_ms':0,'search_metadata':{'status':'disabled','sources':[]}})
    return import_task(created['task_id'])
