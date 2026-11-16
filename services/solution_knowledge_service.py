"""Structured solution candidates, isolated from health causes and product recommendations."""
import hashlib
from urllib.parse import urlparse
from services import model_knowledge_import_service as store

DDL = """CREATE TABLE IF NOT EXISTS solution_knowledge_point (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 batch_id BIGINT UNSIGNED NOT NULL,
 point_key CHAR(64) NOT NULL,
 title VARCHAR(160) NOT NULL,
 problem TEXT NOT NULL,
 applicable_conditions JSON NOT NULL,
 goal TEXT NOT NULL,
 steps JSON NOT NULL,
 record_fields JSON NOT NULL,
 verification JSON NOT NULL,
 stop_conditions JSON NOT NULL,
 source_urls JSON NOT NULL,
 evidence_status VARCHAR(32) NOT NULL,
 review_status ENUM('pending','verified','rejected') NOT NULL DEFAULT 'pending',
 raw_json JSON NOT NULL,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 UNIQUE KEY uk_solution_source (batch_id,point_key),
 KEY idx_solution_batch (batch_id),
 KEY idx_solution_review (review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
JSON_FIELDS = ('applicable_conditions','steps','record_fields','verification','stop_conditions','source_urls')


def text(value, label, maximum):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:
        raise ValueError(label+'缺失或长度无效')
    return value.strip()


def strings(value,label,limit=20):
    if not isinstance(value,list) or len(value)>limit:
        raise ValueError(label+'必须是数组且不能超出条数限制')
    return list(dict.fromkeys(text(v,label,800) for v in value))


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
    with store.connect() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s FOR UPDATE',(batch_id,))
                batch=cur.fetchone()
                if not batch:raise LookupError('采集批次不存在')
                if batch['knowledge_base']!='solution' or batch['status']!='success':
                    raise ValueError('只能导入成功的解决方案知识批次')
                document=validate_document(store.decode(batch['parsed_response']))
                points={hashlib.sha256(store.dumps(p).encode()).hexdigest():p for p in document['solutions']}
                cur.execute('SELECT point_key,review_status FROM solution_knowledge_point WHERE batch_id=%s',(batch_id,))
                existing=list(cur.fetchall())
                unchanged=(batch.get('imported_at') is not None and {p['point_key'] for p in existing}==set(points))
                if not unchanged:
                    if any(p['review_status']!='pending' for p in existing):
                        raise ValueError('解决方案发生变化且已有人工审核记录，请人工处理')
                    cur.execute('DELETE FROM solution_knowledge_point WHERE batch_id=%s',(batch_id,))
                    for key,point in points.items():
                        columns=('title','problem','goal',*JSON_FIELDS,'evidence_status')
                        values=[store.dumps(point[k]) if k in JSON_FIELDS else point[k] for k in columns]
                        cur.execute('INSERT INTO solution_knowledge_point (batch_id,point_key,'+','.join(columns)+',raw_json) VALUES ('+','.join(['%s']*(len(columns)+3))+')',
                                    (batch_id,key,*values,store.dumps(point)))
                cur.execute('UPDATE knowledge_import_batch SET imported_at=COALESCE(imported_at,NOW()),import_error=NULL WHERE id=%s',(batch_id,))
            conn.commit()
        except Exception:
            conn.rollback();raise
    return {'database':store.database_name(),'batch_id':batch_id,'status':'already_imported' if unchanged else 'completed',
            'total_count':len(points),'inserted_count':0 if unchanged else len(points),'source_table':'solution_knowledge_point'}


def task_result(task_id):
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('''SELECT s.*,b.provider source_provider,b.model source_model,b.question source_question
            FROM solution_knowledge_point s JOIN knowledge_import_batch b ON b.id=s.batch_id
            WHERE b.task_id=%s AND b.knowledge_base='solution' ORDER BY s.id''',(task_id,))
        rows=list(cur.fetchall())
    for row in rows:
        row.pop('raw_json',None)
        for key in JSON_FIELDS:row[key]=store.decode(row[key])
    return {'points':rows,'source_table':'solution_knowledge_point'}
