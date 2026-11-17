"""Scene-level health solutions following the supplied health_solution SQL contract."""
import hashlib
import json
from pathlib import Path
import re
from services import model_knowledge_import_service as store

SCHEMA = Path(__file__).resolve().parents[1] / 'vendor/chongxi_health_solution/schema.sql'
GRAIN = ('species','breed','life_stage','need','scenario_code')
FIELDS = (*GRAIN,'scenario','possible_causes','solution','verification')
JSON_FIELDS = {'possible_causes','solution','verification'}
REVIEW_FIELDS = ('review_condition','expected_result','failure_condition','next_action')


def digest(value):return hashlib.sha256(store.dumps(value).encode()).hexdigest()


def raw_text(value, label, maximum=8000):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum:
        raise ValueError(label+'缺失或长度无效')
    return value


def raw_strings(value,label,maximum=40):
    if not isinstance(value,list) or len(value)>maximum:raise ValueError(label+'必须是有限长度的字符串数组')
    return [raw_text(item,label,4000) for item in value]


def validate_scenario(item):
    if not isinstance(item,dict):raise ValueError('场景方案必须是对象')
    result={}
    for key,limit in zip(GRAIN,(32,64,32,64,100)):
        value=item.get(key)
        if key in {'breed','life_stage'} and value is None: value='unspecified'
        value=raw_text(value,key,limit)
        if not re.fullmatch(r'[a-z][a-z0-9_]*',value):raise ValueError(key+'必须为小写英文编码')
        result[key]=value
    if result['species'] not in {'cat','dog'}:raise ValueError('species必须明确为cat或dog')
    result['scenario']=raw_text(item.get('scenario'),'scenario')
    for key in ('possible_causes','solution'):result[key]=raw_strings(item.get(key),key)
    if not result['solution']:raise ValueError('场景方案必须有具体解决动作')
    verification=item.get('verification')
    if not isinstance(verification,dict) or set(verification)!=set(REVIEW_FIELDS):
        raise ValueError('verification必须包含review_condition、expected_result、failure_condition、next_action')
    result['verification']={key:raw_text(verification[key],key,4000) for key in REVIEW_FIELDS}
    return result


def ensure_schema(cur):
    sql='\n'.join(line for line in SCHEMA.read_text().splitlines() if not line.lstrip().startswith('--'))
    cur.execute(sql)
    cur.execute('SHOW COLUMNS FROM health_solution')
    columns={r['Field'] for r in cur.fetchall()}
    if not (set(FIELDS)|{'review_status'}) <= columns:raise ValueError('health_solution原始字段不完整，请检查建表脚本')
    additions={'sources':'JSON NULL','source_count':'INT UNSIGNED NOT NULL DEFAULT 0',
               'model_count':'INT UNSIGNED NOT NULL DEFAULT 0','input_hash':'CHAR(64) NULL'}
    missing=[f'ADD COLUMN `{key}` {definition}' for key,definition in additions.items() if key not in columns]
    if missing:
        import pymysql
        try:cur.execute('ALTER TABLE health_solution '+','.join(missing))
        except pymysql.err.OperationalError as exc:
            if exc.args[0]!=1060:raise


def scenario_key(point):return tuple(point[k] for k in GRAIN)


def from_row(row):
    return {key:store.decode(row[key]) if key in JSON_FIELDS else row[key] for key in FIELDS}


def refresh_groups(conn, affected):
    """Keep complete model variants; never blend steps from different plans."""
    if not affected:return 0
    with conn.cursor() as cur:
        cur.execute('''SELECT p.id,p.batch_id,p.point_key,p.scenario_data,b.task_id,b.provider,b.model,b.question
            FROM solution_knowledge_point_detail p JOIN knowledge_import_batch b ON b.id=p.batch_id
            WHERE p.scenario_data IS NOT NULL AND b.knowledge_base='solution' ORDER BY p.id''')
        grouped={}
        for row in cur.fetchall():
            point=store.decode(row['scenario_data']);key=scenario_key(point)
            if key not in affected:continue
            grouped.setdefault(key,[]).append({'source_type':'model_collection',
                'source_key':'batch:'+str(row['batch_id'])+':'+row['point_key'],
                'candidate_id':row['id'],'batch_id':row['batch_id'],'task_id':row['task_id'],
                'source_provider':row['provider'],'source_model':row['model'],'source_question':row['question'],
                'scenario_data':point})
        changed=0
        for key in sorted(affected):
            cur.execute('SELECT * FROM health_solution WHERE '+ ' AND '.join(k+'=%s' for k in GRAIN)+' FOR UPDATE',key)
            previous=cur.fetchone()
            sources=grouped.get(key,[])
            # Preserve SQL-initialized rows as an explicitly separate source, never as a model result.
            if previous:
                old_sources=store.decode(previous.get('sources')) or []
                initial=[s for s in old_sources if s.get('source_type')!='model_collection']
                if not old_sources:
                    initial=[{'source_type':'sql_initialization','source_key':'initial:'+str(previous['id']),
                              'scenario_data':from_row(previous)}]
                sources=initial+sources
            if not sources:
                if previous and previous['review_status']!='pending_review':raise ValueError('场景方案已有人工审核，不能自动移除')
                if previous:cur.execute('DELETE FROM health_solution WHERE id=%s',(previous['id'],));changed+=1
                continue
            primary=sources[0]['scenario_data']
            payload={'primary':primary,'sources':sources};fingerprint=digest(payload)
            if previous and previous.get('input_hash')==fingerprint:continue
            if previous and previous['review_status']!='pending_review':raise ValueError('场景方案来源或内容变化且已人工审核，请人工处理')
            models={(s['source_provider'],s['source_model']) for s in sources if s.get('source_model')}
            columns=(*FIELDS,'sources','source_count','model_count','input_hash')
            values=[store.dumps(primary[k]) if k in JSON_FIELDS else primary[k] for k in FIELDS]
            values += [store.dumps(sources),len(sources),len(models),fingerprint]
            if previous:
                cur.execute('UPDATE health_solution SET '+','.join(k+'=%s' for k in columns)+' WHERE id=%s',(*values,previous['id']))
            else:
                cur.execute('INSERT INTO health_solution ('+','.join(columns)+') VALUES ('+','.join(['%s']*len(columns))+')',values)
            changed+=1
    return changed
