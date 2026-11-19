"""Routine care source points; raw answers stay in the shared collection batches."""
import hashlib
from services import model_knowledge_import_service as store
from services.behavior_knowledge_dimensions import normalize_scope, load_dictionary
from services.solution_knowledge_service import text, strings
from urllib.parse import urlparse

CATEGORIES={'grooming':'毛发与皮肤清洁','oral_care':'口腔护理','ear_eye_care':'耳眼护理','nail_paw_care':'指甲与足部护理','environment':'居住环境与卫生','exercise':'运动与日常作息','preventive_care':'预防保健与体检','feeding_care':'饮食照护','behavior_care':'行为照护','growth_care':'成长照护','outdoor_care':'户外照护','safety_care':'安全与禁忌','other':'其他养护'}
DDL='''CREATE TABLE IF NOT EXISTS daily_care_knowledge_point_detail (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 species_code VARCHAR(32) NULL,breed_code VARCHAR(255) NULL,life_stage_code VARCHAR(32) NULL,
 raw_scope JSON NOT NULL,need_code VARCHAR(100) NULL,
 care_activity_code VARCHAR(160) NOT NULL,care_activity_name VARCHAR(160) NOT NULL,
 care_subtype VARCHAR(64) NULL,care_domain VARCHAR(64) NULL,care_category VARCHAR(100) NULL,
 care_type VARCHAR(32) NULL,care_objective TEXT NULL,
 applicable_conditions JSON NOT NULL,schedule JSON NULL,tools_materials JSON NULL,
 methods JSON NULL,avoidances JSON NULL,observations JSON NULL,adjustment_rules JSON NULL,
 source_urls JSON NOT NULL,source_ids JSON NOT NULL,extraction_notes JSON NOT NULL,
 evidence_status VARCHAR(64) NOT NULL DEFAULT 'pending_review',
 review_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
 batch_id BIGINT UNSIGNED NOT NULL,point_key CHAR(64) NOT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uk_care_source(batch_id,point_key),KEY idx_care_scope(species_code,care_domain)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''
JSON_FIELDS=('raw_scope','applicable_conditions','schedule','tools_materials','methods','avoidances',
             'observations','adjustment_rules','source_urls','source_ids','extraction_notes')
DOMAIN_CATEGORIES={k+'_care' if k in {'grooming','environment'} else k:v for k,v in CATEGORIES.items()}


def ensure_schema(cur):
    cur.execute(DDL)
    cur.execute('SHOW COLUMNS FROM daily_care_knowledge_point_detail')
    if 'point_data' in {r['Field'] for r in cur.fetchall()}:
        raise RuntimeError('请先运行 scripts/migrate_daily_care_source_fusion.py --apply 迁移养护来源表')
    from services.daily_care_fusion_service import ensure_schema as ensure_fusion_schema
    ensure_fusion_schema(cur)
    cur.execute('''CREATE OR REPLACE VIEW daily_care_knowledge_source_view AS
      SELECT p.*,b.task_id,b.provider source_provider,b.model source_model,b.question source_question
      FROM daily_care_knowledge_point_detail p JOIN knowledge_import_batch b ON b.id=p.batch_id''')


def decode_row(row):
    for key in JSON_FIELDS:
        if key in row:row[key]=store.decode(row[key])
    return row


def source_row(point,dictionary):
    from services.daily_care_activity_adapter import dictionary_scope
    scope=normalize_scope(dictionary_scope(point) if point.get('record_type')=='care_activity' else point,dictionary)
    activity=point.get('care_activity')
    raw={k:point.get(k) for k in ('species','breed','life_stage')}
    row={k:scope[k] for k in ('species_code','breed_code','life_stage_code')}
    row.update(raw_scope=raw,care_activity_code=point['care_code'],care_activity_name=point['care_name'],
               need_code=point.get('need_code','unknown'),care_subtype=point.get('care_subtype','unknown'))
    if activity is not None:
        for key in ('care_domain','care_category','care_type','care_objective','applicable_conditions','schedule',
                    'tools_materials','methods','avoidances','observations','adjustment_rules'):
            row[key]=activity.get(key)
        row['applicable_conditions']=row['applicable_conditions'] if row['applicable_conditions'] is not None else []
        row['source_ids']=activity.get('source_ids') or []
        known={'species','breed','life_stage','need_code','care_activity_code','care_activity_name','care_subtype','care_domain','care_category','care_type','care_objective','applicable_conditions','schedule','tools_materials','methods','avoidances','observations','adjustment_rules','source_urls','source_ids','evidence_status'}
        row['extraction_notes']={'format':'care_activities','source_scope':point.get('source_scope'),
                                'extra_fields':{k:v for k,v in activity.items() if k not in known},
                                'dimension_mapping':scope['summary_meta']}
    else:
        category=point['category_code']
        row.update(care_domain={'grooming':'grooming_care','environment':'environment_care'}.get(category,category),
                   care_category=CATEGORIES[category],care_type=None,care_objective=None,
                   applicable_conditions=point['applicable_conditions'],schedule={'frequency_text':point['frequency']},
                   tools_materials=None,methods=point['steps'],avoidances=point['precautions'],
                   observations={'unspecified':point['observation_fields']},adjustment_rules=point['stop_conditions'],source_ids=[])
        row['extraction_notes']={'format':'legacy_care_points','dimension_mapping':scope['summary_meta'],
                                'migration':'frequency->schedule.frequency_text; observation_fields->observations.unspecified'}
    row.update(source_urls=point['source_urls'],evidence_status=point['evidence_status'])
    return row


def validate_document(value):
    if isinstance(value,dict) and 'care_activities' in value:
        if 'care_points' in value:raise ValueError('care_activities与care_points不能同时出现，请明确使用一种格式')
        from services.daily_care_activity_adapter import points_for
        if value.get('knowledge_base') not in (None,'daily_care'):raise ValueError('养护文档 knowledge_base 不匹配')
        points_for(value)
        return {**value,'knowledge_base':'daily_care'}
    if not isinstance(value,dict) or value.get('knowledge_base')!='daily_care':raise ValueError('养护文档须指定 knowledge_base=daily_care')
    question=text(value.get('question'),'question',2000)
    points=value.get('care_points')
    if not isinstance(points,list) or not 1<=len(points)<=30:raise ValueError('care_points须为1至30条养护知识')
    result=[];seen=set()
    for item in points:
        if not isinstance(item,dict):raise ValueError('养护知识必须为对象')
        p={k:text(item.get(k),k,160 if k in ('care_code','care_name') else 800) for k in ('species','breed','life_stage','care_code','care_name','frequency')}
        if p['species'] not in {'cat','dog','unknown'}:raise ValueError('物种须为cat/dog/unknown')
        p['category_code']=item.get('category_code')
        if p['category_code'] not in CATEGORIES:raise ValueError('养护分类无效')
        for k in ('applicable_conditions','observation_fields','precautions','stop_conditions','source_urls'):
            p[k]=strings(item.get(k),k)
        if not p['applicable_conditions'] or not p['stop_conditions']:raise ValueError('须说明适用条件及停止/就医条件')
        steps=item.get('steps')
        if not isinstance(steps,list) or not 1<=len(steps)<=15:raise ValueError('养护步骤须为1至15条')
        p['steps']=[]
        for step in steps:
            if not isinstance(step,dict):raise ValueError('养护步骤须为对象')
            p['steps'].append({k:text(step.get(k),k,1000) for k in ('action','reason')})
        for url in p['source_urls']:
            u=urlparse(url)
            if u.scheme not in {'http','https'} or not u.netloc:raise ValueError('来源须为HTTP/HTTPS网址')
        p['evidence_status']=item.get('evidence_status','pending_verification')
        if p['evidence_status'] not in {'supported','uncertain','pending_verification'}:raise ValueError('证据状态无效')
        key=store.dumps(p)
        if key not in seen:result.append(p);seen.add(key)
    return {'knowledge_base':'daily_care','question':question,'care_points':result}


def import_batch(batch_id):
    store.ensure_database()
    dictionary=load_dictionary()
    from services import daily_care_fusion_service as fusion
    with store.connect() as conn:
        lock=None
        try:
            with conn.cursor() as cur:
                lock=fusion.acquire_lock(cur)
                cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s FOR UPDATE',(batch_id,));batch=cur.fetchone()
                if not batch or batch['knowledge_base']!='daily_care' or batch['status']!='success':raise ValueError('只能解析成功的日常养护批次')
                metadata=store.decode(batch.get('search_metadata')) or {}
                if metadata.get('response_status')=='incomplete':
                    raise ValueError('模型回答被截断，原文已保留；请提高输出上限后重新采集该模型，重复解析无法补全缺失内容')
                from vendor.chongxi_multi_model_mysql.run import extract_json
                doc=extract_json(batch['raw_response'] or '',knowledge_base='daily_care',question=batch['question'])
                from services.daily_care_activity_adapter import points_for
                source_points=points_for(doc) if 'care_activities' in doc else doc['care_points']
                points={hashlib.sha256(store.dumps(p).encode()).hexdigest():p for p in source_points}
                cur.execute('SELECT point_key,review_status FROM daily_care_knowledge_point_detail WHERE batch_id=%s',(batch_id,));old_rows=list(cur.fetchall());old={p['point_key'] for p in old_rows}
                if old!=set(points) and any(p['review_status']!='pending' for p in old_rows):raise ValueError('已有人工审核记录，请人工处理内容变化')
                for key in old-set(points):cur.execute('DELETE FROM daily_care_knowledge_point_detail WHERE batch_id=%s AND point_key=%s',(batch_id,key))
                for key in set(points)-old:
                    row={**source_row(points[key],dictionary),'batch_id':batch_id,'point_key':key}
                    cols=list(row)
                    cur.execute('INSERT INTO daily_care_knowledge_point_detail ('+','.join(cols)+') VALUES ('+','.join(['%s']*len(cols))+')',tuple(store.dumps(row[k]) if k in JSON_FIELDS else row[k] for k in cols))
                fused=fusion.rebuild(cur)
                cur.execute('UPDATE knowledge_import_batch SET imported_at=COALESCE(imported_at,NOW()),import_error=NULL WHERE id=%s',(batch_id,))
            conn.commit()
        except Exception:
            conn.rollback();raise
        finally:
            if lock:
                with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))
    return {'batch_id':batch_id,'status':'already_imported' if old==set(points) else 'completed','total_count':len(points),'inserted_count':len(set(points)-old),'fusion':fused}


def list_points(*,page=1,query='',species='',category='',task_id=None,view='source'):
    try:page=int(page)
    except (ValueError,TypeError):raise ValueError('页码无效')
    if not 1<=page<=100000 or len(query)>200:raise ValueError('页码或检索词无效')
    if species and species not in {'cat','dog','unknown'}:raise ValueError('物种无效')
    if category and category not in DOMAIN_CATEGORIES:raise ValueError('养护分类无效')
    if view not in {'source','fused'}:raise ValueError('养护视图无效')
    store.ensure_database();clauses=[];params=[]
    if view=='source':
        table='daily_care_knowledge_point_detail p JOIN knowledge_import_batch b ON b.id=p.batch_id'
        clauses=["b.knowledge_base='daily_care'","b.status='success'"]
        select='p.*,b.task_id,b.provider source_provider,b.model source_model,b.question source_question'
    else:
        table='daily_care_knowledge_point p';clauses=['p.active=1'];select='p.*'
    for field,v in (('p.species_code',species),('p.care_domain',category)):
        if field=='p.species_code' and v=='unknown':clauses.append(field+' IS NULL')
        elif v is not None and v!='':clauses.append(field+'=%s');params.append(v)
    if task_id is not None:
        if view=='source':clauses.append('b.task_id=%s')
        else:clauses.append('EXISTS (SELECT 1 FROM daily_care_knowledge_point_source l JOIN daily_care_knowledge_point_detail d ON d.id=l.source_detail_id JOIN knowledge_import_batch b ON b.id=d.batch_id WHERE l.knowledge_point_id=p.id AND b.task_id=%s)')
        params.append(task_id)
    if query:
        search_fields=('care_activity_code','care_activity_name','need_code','care_subtype','care_domain','care_category','care_objective','applicable_conditions','schedule','tools_materials','methods','avoidances','observations','adjustment_rules')
        clauses.append('('+' OR '.join("CAST(p.`"+f+"` AS CHAR) LIKE %s ESCAPE '!'" for f in search_fields)+')')
        params+=['%'+query.replace('!','!!').replace('%','!%').replace('_','!_')+'%']*len(search_fields)
    page_size=100 if task_id is not None else 20
    join=' FROM '+table+' WHERE '+' AND '.join(clauses)
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) n'+join,params);total=cur.fetchone()['n']
        cur.execute('SELECT '+select+join+' ORDER BY p.id DESC LIMIT %s OFFSET %s',params+[page_size,(page-1)*page_size]);rows=list(cur.fetchall())
        if view=='fused':
            from services.daily_care_fusion_service import JSON_FIELDS as fusion_json
            for row in rows:
                for key in fusion_json:
                    if key in row:row[key]=store.decode(row[key])
                cur.execute('''SELECT l.source_detail_id,d.batch_id,b.task_id,b.provider,b.model,d.review_status
                    FROM daily_care_knowledge_point_source l JOIN daily_care_knowledge_point_detail d ON d.id=l.source_detail_id
                    JOIN knowledge_import_batch b ON b.id=d.batch_id WHERE l.knowledge_point_id=%s ORDER BY d.id''',(row['id'],))
                row['sources']=list(cur.fetchall())
        else:
            rows=[decode_row(row) for row in rows]
    return {'items':rows,'total':total,'page':page,'page_size':page_size,'categories':DOMAIN_CATEGORIES,'view':view}


def render_prompt(template,question):
    import re
    values={'question':question,'species':'unknown','breed':'unknown','life_stage':'unknown',
            'existing_care_activities':'[]','existing_observations':'[]',
            'reference_materials':'未提供可信参考资料；不得编造来源ID，待审核'}
    return re.sub(r'\{\{(question|species|breed|life_stage|existing_care_activities|existing_observations|reference_materials)\}\}',
                  lambda match:values[match.group(1)],template)
