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
 care_code VARCHAR(160) NOT NULL,care_name VARCHAR(160) NOT NULL,
 category_code VARCHAR(32) NOT NULL,category_name VARCHAR(255) NOT NULL,
 applicable_conditions JSON NOT NULL,frequency TEXT NOT NULL,steps JSON NOT NULL,
 observation_fields JSON NOT NULL,precautions JSON NOT NULL,stop_conditions JSON NOT NULL,
 evidence_status VARCHAR(32) NOT NULL DEFAULT 'candidate',
 review_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
 source_urls JSON NOT NULL,summary_meta JSON NOT NULL,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 batch_id BIGINT UNSIGNED NOT NULL,point_key CHAR(64) NOT NULL,point_data JSON NOT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE KEY uk_care_source(batch_id,point_key),KEY idx_care_scope(species_code,category_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''
from services.daily_care_activity_adapter import ACTIVITY_FIELDS, ACTIVITY_JSON_FIELDS
DDL=DDL.replace(' applicable_conditions JSON', ''.join(' '+name+' '+definition+',\n' for name,definition in ACTIVITY_FIELDS.items())+' applicable_conditions JSON')
JSON_FIELDS=(*ACTIVITY_JSON_FIELDS,'applicable_conditions','steps','observation_fields','precautions','stop_conditions','source_urls','summary_meta','point_data')

def ensure_schema(cur):
    cur.execute(DDL)
    cur.execute('SHOW COLUMNS FROM daily_care_knowledge_point_detail')
    existing={r['Field'] for r in cur.fetchall()}
    previous='category_name'
    for name,definition in ACTIVITY_FIELDS.items():
        if name not in existing:
            import pymysql
            try:cur.execute('ALTER TABLE daily_care_knowledge_point_detail ADD COLUMN `'+name+'` '+definition+' AFTER `'+previous+'`')
            except pymysql.err.OperationalError as exc:
                if exc.args[0]!=1060:raise
        previous=name


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
    with store.connect() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s FOR UPDATE',(batch_id,));batch=cur.fetchone()
                if not batch or batch['knowledge_base']!='daily_care' or batch['status']!='success':raise ValueError('只能解析成功的日常养护批次')
                from vendor.chongxi_multi_model_mysql.run import extract_json
                doc=extract_json(batch['raw_response'] or '',knowledge_base='daily_care',question=batch['question'])
                from services.daily_care_activity_adapter import points_for, dictionary_scope
                source_points=points_for(doc) if 'care_activities' in doc else doc['care_points']
                points={hashlib.sha256(store.dumps(p).encode()).hexdigest():p for p in source_points}
                cur.execute('SELECT point_key,review_status FROM daily_care_knowledge_point_detail WHERE batch_id=%s',(batch_id,));old_rows=list(cur.fetchall());old={p['point_key'] for p in old_rows}
                if old!=set(points) and any(p['review_status']!='pending' for p in old_rows):raise ValueError('已有人工审核记录，请人工处理内容变化')
                for key in old-set(points):cur.execute('DELETE FROM daily_care_knowledge_point_detail WHERE batch_id=%s AND point_key=%s',(batch_id,key))
                for key in set(points)-old:
                    p=points[key];scope=normalize_scope(dictionary_scope(p) if p.get('record_type')=='care_activity' else p,dictionary)
                    scope['summary_meta']['raw_scope']={k:p.get(k) for k in ('species','breed','life_stage')}
                    row={**{k:p[k] for k in ('care_code','care_name','category_code','applicable_conditions','frequency','steps','observation_fields','precautions','stop_conditions','source_urls')},**scope,
                         'category_name':CATEGORIES[p['category_code']],'batch_id':batch_id,'point_key':key,'point_data':p}
                    row['summary_meta']['raw_evidence_status']=p['evidence_status']
                    cols=list(row)
                    cur.execute('INSERT INTO daily_care_knowledge_point_detail ('+','.join(cols)+') VALUES ('+','.join(['%s']*len(cols))+')',tuple(store.dumps(row[k]) if k in JSON_FIELDS else row[k] for k in cols))
                cur.execute('UPDATE knowledge_import_batch SET imported_at=COALESCE(imported_at,NOW()),import_error=NULL WHERE id=%s',(batch_id,))
            conn.commit()
        except Exception:
            conn.rollback();raise
    return {'batch_id':batch_id,'status':'already_imported' if old==set(points) else 'completed','total_count':len(points),'inserted_count':len(set(points)-old)}


def list_points(*,page=1,query='',species='',category='',task_id=None):
    try:page=int(page)
    except (ValueError,TypeError):raise ValueError('页码无效')
    if not 1<=page<=100000 or len(query)>200:raise ValueError('页码或检索词无效')
    if species and species not in {'cat','dog','unknown'}:raise ValueError('物种无效')
    if category and category not in CATEGORIES:raise ValueError('养护分类无效')
    store.ensure_database();clauses=["b.knowledge_base='daily_care'","b.status='success'"];params=[]
    for field,v in (('p.species_code',species),('p.category_code',category),('b.task_id',task_id)):
        if field=='p.species_code' and v=='unknown':clauses.append(field+' IS NULL')
        elif v is not None and v!='':clauses.append(field+'=%s');params.append(v)
    if query:
        clauses.append("CAST(p.point_data AS CHAR) LIKE %s ESCAPE '!'")
        params.append('%'+query.replace('!','!!').replace('%','!%').replace('_','!_')+'%')
    page_size=100 if task_id is not None else 20
    join=' FROM daily_care_knowledge_point_detail p JOIN knowledge_import_batch b ON b.id=p.batch_id WHERE '+' AND '.join(clauses)
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) n'+join,params);total=cur.fetchone()['n']
        cur.execute('SELECT p.*,b.task_id,b.provider source_provider,b.model source_model,b.question source_question'+join+' ORDER BY p.id DESC LIMIT %s OFFSET %s',params+[page_size,(page-1)*page_size]);rows=list(cur.fetchall())
    for row in rows:
        for k in JSON_FIELDS:row[k]=store.decode(row[k])
    return {'items':rows,'total':total,'page':page,'page_size':page_size,'categories':CATEGORIES}


def render_prompt(template,question):
    import re
    values={'question':question,'species':'unknown','breed':'unknown','life_stage':'unknown',
            'existing_care_activities':'[]','existing_observations':'[]',
            'reference_materials':'未提供可信参考资料；不得编造来源ID，待审核'}
    return re.sub(r'\{\{(question|species|breed|life_stage|existing_care_activities|existing_observations|reference_materials)\}\}',
                  lambda match:values[match.group(1)],template)
