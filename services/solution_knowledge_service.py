"""Structured solution candidates, isolated from health causes and product recommendations."""
import hashlib
from urllib.parse import urlparse
from services import model_knowledge_import_service as store

LEGACY_JSON_FIELDS = ('applicable_conditions','steps','record_fields','verification','stop_conditions','source_urls')
JSON_FIELDS = (*LEGACY_JSON_FIELDS, 'scenario_data')


def text(value, label, maximum):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:
        raise ValueError(label+'缺失或长度无效')
    return value.strip()


def strings(value,label,limit=20):
    if not isinstance(value,list) or len(value)>limit:
        raise ValueError(label+'必须是数组且不能超出条数限制')
    return list(dict.fromkeys(text(v,label,800) for v in value))


def ensure_parse_column(cur):
    cur.execute("SHOW COLUMNS FROM knowledge_import_batch LIKE 'solution_parse_result'")
    if not cur.fetchone():
        import pymysql
        try:cur.execute('ALTER TABLE knowledge_import_batch ADD COLUMN solution_parse_result JSON NULL')
        except pymysql.err.OperationalError as exc:
            if exc.args[0]!=1060:raise


def ensure_schema(cur):
    ensure_parse_column(cur)
    cur.execute("SELECT COUNT(*) n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='solution_knowledge_point_detail'")
    if cur.fetchone()['n']:
        raise ValueError('请先运行 scripts/remove_solution_source_detail.py --apply 迁移解决方案来源')
    from services.health_solution_service import ensure_schema as ensure_health_schema
    ensure_health_schema(cur)
    from services.solution_scene_service import ensure_schema as ensure_scene_schema
    ensure_scene_schema(cur)


def source_rows(cur,task_id=None):
    sql="SELECT id,task_id,provider,model,question,solution_parse_result FROM knowledge_import_batch WHERE knowledge_base='solution' AND status='success'"
    params=[]
    if task_id is not None:sql+=' AND task_id=%s';params.append(task_id)
    cur.execute(sql+' ORDER BY id',params)
    rows=[]
    for batch in cur.fetchall():
        for point in (store.decode(batch.get('solution_parse_result')) or {}).get('points',[]):
            rows.append({**point,'batch_id':batch['id'],'task_id':batch['task_id'],
                         'provider':batch['provider'],'model':batch['model'],'question':batch['question']})
    # Preserve historical representative selection by the original candidate IDs.
    return sorted(rows,key=lambda r:(0,r['id']) if type(r['id']) is int else (1,r['batch_id'],r['point_key']))


def scenario_point(item):
    from services.health_solution_service import validate_scenario
    result=validate_scenario(item)
    result['source_urls']=strings(item.get('source_urls',[]),'source_urls')
    for url in result['source_urls']:
        parsed=urlparse(url)
        if parsed.scheme not in {'http','https'} or not parsed.netloc:raise ValueError('来源链接必须是HTTP或HTTPS网址')
    evidence=item.get('evidence_status','pending_verification')
    if evidence not in {'supported','uncertain','pending_verification'}:raise ValueError('解决方案证据状态无效')
    result['evidence_status']=evidence
    for key in ('record_fields','stop_conditions'):
        if key in item:result[key]=strings(item[key],key)
    return result


def candidate_values(point):
    if 'scenario_code' not in point:return {**point,'scenario_data':None}
    from services.health_solution_service import FIELDS
    scene={k:point[k] for k in FIELDS}
    return {'title':point['scenario_code'], 'problem':point['scenario'], 'goal':'',
            'applicable_conditions':[point[k] for k in ('species','breed','life_stage','need')],
            'steps':[{'action':action,'reason':''} for action in point['solution']],
            'record_fields':[], 'verification':[], 'stop_conditions':[],
            'source_urls':point['source_urls'], 'evidence_status':point['evidence_status'],'scenario_data':scene}


def validate_document(value):
    if not isinstance(value,dict) or value.get('knowledge_base')!='solution':
        raise ValueError('解决方案文档必须指定 knowledge_base=solution')
    question=text(value.get('question'),'question',2000)
    points=value.get('solutions')
    if not isinstance(points,list) or len(points)>30:
        raise ValueError('solutions必须为数组，最多30条')
    validated=[];seen=set()
    for item in points:
        if not isinstance(item,dict):raise ValueError('解决方案必须为对象')
        if 'scenario' in item or 'scenario_code' in item:
            result=scenario_point(item)
            fingerprint=hashlib.sha256(store.dumps(result).encode()).hexdigest()
            if fingerprint not in seen:validated.append(result);seen.add(fingerprint)
            continue
        result={key:text(item.get(key),key,160 if key=='title' else 2000) for key in ('title','problem','goal')}
        for key in ('applicable_conditions','record_fields','verification','stop_conditions','source_urls'):
            result[key]=strings(item.get(key),key)
        if not result['applicable_conditions']:raise ValueError('必须明确适用条件')
        for url in result['source_urls']:
            parsed=urlparse(url)
            if parsed.scheme not in {'https','http'} or not parsed.netloc:
                raise ValueError('来源链接必须是HTTP或HTTPS网址')
        steps=item.get('steps')
        if not isinstance(steps,list) or not 1<=len(steps)<=15:
            raise ValueError('操作步骤必须为1至15条')
        result['steps']=[]
        for step in steps:
            if not isinstance(step,dict):raise ValueError('每步必须包含action和reason')
            result['steps'].append({'action':text(step.get('action'),'action',1500),
                                    'reason':text(step.get('reason'),'reason',1000)})
        evidence=item.get('evidence_status')
        if evidence not in {'supported','uncertain','pending_verification'}:
            raise ValueError('解决方案证据状态无效')
        result['evidence_status']=evidence
        fingerprint=hashlib.sha256(store.dumps(result).encode()).hexdigest()
        if fingerprint not in seen:validated.append(result);seen.add(fingerprint)
    return {'knowledge_base':'solution','question':question,'solutions':validated}


def import_batch(batch_id):
    store.ensure_database()
    lock='solution-import:'+hashlib.sha256(store.database_name().encode()).hexdigest()[:40]
    with store.connect() as conn:
        acquired=False
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT GET_LOCK(%s,10) acquired',(lock,))
                acquired=bool(cur.fetchone()['acquired'])
                if not acquired:raise ValueError('解决方案入库繁忙，请稍后重试')
                cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s FOR UPDATE',(batch_id,))
                batch=cur.fetchone()
                if not batch:raise LookupError('采集批次不存在')
                if batch['knowledge_base']!='solution' or batch['status']!='success':
                    raise ValueError('只能导入成功的解决方案知识批次')
                document=validate_document(store.decode(batch['parsed_response']))
                points={hashlib.sha256(store.dumps(p).encode()).hexdigest():p for p in document['solutions']}
                previous=store.decode(batch.get('solution_parse_result')) or {}
                existing=previous.get('points',[])
                from services.health_solution_service import scenario_key, refresh_groups
                affected={scenario_key(store.decode(p['scenario_data'])) for p in existing if p.get('scenario_data')}
                affected.update(scenario_key(p) for p in points.values() if 'scenario_code' in p)
                old_keys={p['point_key'] for p in existing}
                unchanged=(batch.get('imported_at') is not None and old_keys==set(points))
                inserted=0
                if not unchanged:
                    if any(p['review_status']!='pending' for p in existing):
                        raise ValueError('解决方案发生变化且已有人工审核记录，请人工处理')
                    from datetime import datetime
                    now=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    old={p['point_key']:p for p in existing};saved=[]
                    for key,point in points.items():
                        if key in old:saved.append(old[key]);continue
                        saved.append({**candidate_values(point),'id':'batch:'+str(batch_id)+':'+key,
                                      'batch_id':batch_id,'point_key':key,'raw_json':point,'review_status':'pending',
                                      'created_at':now,'updated_at':now})
                        inserted+=1
                    result={'version':1,'points':saved,'point_count':len(saved),'processed_at':now}
                    cur.execute('UPDATE knowledge_import_batch SET solution_parse_result=%s WHERE id=%s',
                                (store.dumps(result),batch_id))
                scenario_updates=refresh_groups(conn,affected)
                from services.solution_scene_service import refresh
                scene_result=refresh(conn)
                cur.execute('UPDATE knowledge_import_batch SET imported_at=COALESCE(imported_at,NOW()),import_error=NULL WHERE id=%s',(batch_id,))
            conn.commit()
        except Exception:
            conn.rollback();raise
        finally:
            if acquired:
                with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))
    return {'database':store.database_name(),'batch_id':batch_id,'status':'already_imported' if unchanged else 'completed',
            'total_count':len(points),'inserted_count':inserted,'source_table':'knowledge_import_batch','source_field':'solution_parse_result',
            'scenario_table':'solution_knowledge_point','scenario_count':scene_result['scene_count'],
            'scenario_updates':scene_result['changed_count'],'legacy_scenario_updates':scenario_updates}


def task_result(task_id):
    with store.connect() as conn,conn.cursor() as cur:
        rows=source_rows(cur,task_id)
    for row in rows:
        row.update(source_provider=row['provider'],source_model=row['model'],source_question=row['question'])
        row.pop('raw_json',None)
        for key in JSON_FIELDS:row[key]=store.decode(row.get(key))
    scenes=[r for r in rows if r.get('scenario_data')]
    if scenes:
        from services.health_solution_service import GRAIN, scenario_key
        with store.connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT id,'+','.join(GRAIN)+',review_status FROM health_solution')
            native={scenario_key(r):r for r in cur.fetchall()}
        for row in scenes:
            scene=native.get(scenario_key(row['scenario_data'])) or {}
            row['health_solution_id']=scene.get('id')
            row['scenario_review_status']=scene.get('review_status')
    from services.solution_scene_service import task_points
    points=task_points(task_id)
    return {'points':points,'source_table':'solution_knowledge_point',
            'source_points':rows,'source_detail_table':'knowledge_import_batch','source_detail_field':'solution_parse_result.points',
            'scenario_table':'solution_knowledge_point','scenario_count':len(points)}
