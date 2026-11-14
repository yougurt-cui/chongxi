"""Admin collection jobs stored only in the independent two-table knowledge store."""
from __future__ import annotations
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from services import model_knowledge_import_service as store
from vendor.chongxi_multi_model_mysql import run as collector

ROOT=Path(__file__).resolve().parents[1]
BASE_CONFIG_PATH=collector.ROOT/'knowledge_bases.json'


def ensure_tables():return store.ensure_database()


def knowledge_bases():
    from services.multi_model_prompt_service import collection_bases
    return collection_bases()


def base_map():
    return {b['code']:b for b in knowledge_bases()['bases']}


def resolve_base(code):
    config=knowledge_bases();known=base_map()
    if not code:code=config['default_base']
    if not isinstance(code,str) or code not in known:raise ValueError('知识库不存在')
    return code,known[code]


def configuration():
    from services.model_native_search_service import supports_search,search_model
    config=knowledge_bases();default=base_map()[config['default_base']]
    return {'template':default['templates'][0]['template'],
        'knowledge_base':default['code'],
        'knowledge_bases':config['bases'],
        'knowledge_database':store.database_name(),
        'providers':[{'name':p.name,'model':p.model,'key_configured':bool(p.key),'ready':bool(p.key and p.model),
                      'search_supported':supports_search(p),'search_model':search_model(p) if supports_search(p) else ''}
                     for p in collector.provider_configuration()]}


def validate(payload):
    if not isinstance(payload,dict):raise ValueError('提交内容必须为对象')
    base_code,base=resolve_base(payload.get('knowledge_base'))
    question=payload.get('question')
    if not isinstance(question,str) or not 1<=len(question.strip())<=2000:raise ValueError('问题须为1–2000字')
    template_code=payload.get('template_code')
    if template_code:
        matched=[t for t in base['templates'] if t['code']==template_code]
        if not matched:raise ValueError('该知识库下没有这个模版')
        template=matched[0]['template']
    else:template=payload.get('template')
    if not isinstance(template,str) or not 1<=len(template)<=30000 or '{{question}}' not in template:
        raise ValueError('模板须包含 {{question}} 且不超过30000字')
    names=payload.get('providers')
    if not isinstance(names,list) or not names or len(names)>3 or any(not isinstance(x,str) for x in names) or len(set(names))!=len(names):
        raise ValueError('请选择1–3个模型')
    available={p.name:p for p in collector.provider_configuration()}
    if any(n not in available for n in names):raise ValueError('包含不支持的模型')
    providers=[available[n] for n in names]
    if any(not p.key or not p.model for p in providers):raise ValueError('所选模型缺少密钥或模型ID')
    web_search=payload.get('web_search',False)
    if not isinstance(web_search,bool):raise ValueError('联网搜索开关必须为布尔值')
    if web_search:
        from services.model_native_search_service import search_model
        providers=[collector.Provider(p.name,p.key,p.base_url,search_model(p)) for p in providers]
    return question.strip(),template,providers,base_code


def expire(cur):
    cur.execute("""UPDATE knowledge_import_batch SET status='api_failed',error_message='执行超时或任务中断',
        finished_at=NOW() WHERE status='running' AND created_at < NOW()-INTERVAL 30 MINUTE""")


def create_task(payload,launch=True,file_import=False):
    if file_import:
        question=payload['question'];template=payload['template'];providers=[collector.Provider('file','','','json-file')]
        base_code,_=resolve_base(payload.get('knowledge_base'))
    else:question,template,providers,base_code=validate(payload)
    ensure_tables();conn=store.connect();acquired=False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK('chongxi:model-collection-submit',5) AS acquired")
            acquired=bool(cur.fetchone()['acquired'])
            if not acquired:raise ValueError('提交繁忙，请稍后重试')
            expire(cur)
            cur.execute("SELECT COUNT(DISTINCT task_id) AS n FROM knowledge_import_batch WHERE status='running'")
            if cur.fetchone()['n']>=2:raise ValueError('已有两个采集任务运行中，请等待完成')
            task_id=None
            for p in providers:
                cur.execute('''INSERT INTO knowledge_import_batch (task_id,provider,model,question,prompt_template,web_search,knowledge_base)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)''',(task_id or 0,p.name,p.model,question,template,
                    int(payload.get('web_search',False)),base_code))
                if task_id is None:
                    task_id=cur.lastrowid
                    cur.execute('UPDATE knowledge_import_batch SET task_id=%s WHERE id=%s',(task_id,task_id))
        conn.commit()
        if launch:
            try:
                log_dir=ROOT/'var/multi-model';log_dir.mkdir(parents=True,exist_ok=True)
                with (log_dir/f'{task_id}.log').open('ab') as log:
                    subprocess.Popen([sys.executable,str(ROOT/'scripts/run_multi_model_task.py'),str(task_id)],cwd=ROOT,
                        stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
            except Exception:
                fail_running(task_id,'任务进程启动失败');raise
    except Exception:conn.rollback();raise
    finally:
        if acquired:
            with conn.cursor() as cur:cur.execute("SELECT RELEASE_LOCK('chongxi:model-collection-submit')")
        conn.close()
    return {'task_id':task_id,'status':'running'}


def submit(payload):return create_task(payload)


def fail_running(task_id,message='任务执行中断，请检查日志'):
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE knowledge_import_batch SET status='api_failed',error_message=%s,finished_at=NOW() WHERE task_id=%s AND status='running'",(message,task_id))
        conn.commit()


def aggregate_status(rows):
    states=[r['status'] for r in rows]
    if 'running' in states:return 'running'
    if all(s=='success' for s in states):return 'completed'
    return 'partial_failed' if 'success' in states else 'failed'


def list_tasks(page=1,knowledge_base=''):
    try:page=int(page)
    except (ValueError,TypeError):raise ValueError('页码不正确')
    if not 1<=page<=100000:raise ValueError('页码不正确')
    where='';params=[]
    if knowledge_base:
        base_code,_=resolve_base(knowledge_base)
        where=' WHERE b.knowledge_base=%s';params=[base_code]
    ensure_tables()
    with store.connect() as conn:
        with conn.cursor() as cur:
            expire(cur)
            cur.execute('SELECT COUNT(DISTINCT b.task_id) AS n FROM knowledge_import_batch b'+where,params);total=cur.fetchone()['n']
            cur.execute('''SELECT b.task_id AS id,MAX(b.question) AS question,MIN(b.created_at) AS created_at,
                MAX(b.knowledge_base) AS knowledge_base,
                CASE WHEN SUM(b.status='running')>0 THEN 'running'
                     WHEN SUM(b.status='success')=COUNT(*) THEN 'completed'
                     WHEN SUM(b.status='success')>0 THEN 'partial_failed' ELSE 'failed' END AS status,
                MAX(b.finished_at) AS finished_at,
                CASE WHEN MAX(b.knowledge_base)='health' THEN
                (SELECT COUNT(*) FROM health_knowledge_point_detail h JOIN knowledge_import_batch b2 ON b2.id=h.batch_id WHERE b2.task_id=b.task_id)
                ELSE (SELECT COUNT(*) FROM knowledge_point_source_detail s JOIN knowledge_import_batch b2 ON b2.id=s.batch_id
                 WHERE b2.task_id=b.task_id) END AS point_count
                FROM knowledge_import_batch b'''+where+''' GROUP BY b.task_id ORDER BY b.task_id DESC LIMIT 20 OFFSET %s''',params+[(page-1)*20])
            items=list(cur.fetchall())
        conn.commit()
    return {'items':items,'total':total,'page':page,'page_size':20,'knowledge_base':knowledge_base}


def task_detail(task_id):
    ensure_tables()
    with store.connect() as conn:
        with conn.cursor() as cur:
            expire(cur)
            cur.execute('SELECT * FROM knowledge_import_batch WHERE task_id=%s ORDER BY id',(task_id,));rows=list(cur.fetchall())
            if not rows:raise LookupError('任务不存在')
            cur.execute('''SELECT s.*,b.provider,b.model FROM knowledge_point_source_detail s
                JOIN knowledge_import_batch b ON b.id=s.batch_id WHERE b.task_id=%s ORDER BY s.id''',(task_id,));points=list(cur.fetchall())
        conn.commit()
    first=rows[0]
    task={'id':task_id,'question':first['question'],'template_text':first['prompt_template'],
          'web_search':bool(first['web_search']),'status':aggregate_status(rows),
          'knowledge_base':first.get('knowledge_base') or 'selection'}
    responses=[]
    for row in rows:
        responses.append({'id':row['id'],'provider':row['provider'],'model_name':row['model'],'status':row['status'],
            'raw_text':row['raw_response'],'parsed_json':store.decode(row['parsed_response']),
            'search_metadata':store.decode(row['search_metadata']) or {'status':'not_confirmed' if row['web_search'] else 'disabled','sources':[]},
            'error_message':row['error_message'],'import_error':row['import_error'],
            'prompt_tokens':row['prompt_tokens'],'completion_tokens':row['completion_tokens']})
    for point in points:
        for field in ('life_stage_codes','need_codes','standardization_meta'):
            point[field]=store.decode(point.get(field))
    result = {'task':task,'responses':responses,'points':points,'knowledge_import':store.task_status(task_id)}
    if task['knowledge_base'] == 'health':
        from services.health_knowledge_import_service import task_result
        result['health_structured'] = task_result(task_id)
    from services.knowledge_source_assembly_service import task_objects
    result['assembled_points'] = task_objects(task_id)
    return result


def run_worker(task_id):
    lock_name='model-worker:'+hashlib.sha256((store.database_name()+':'+str(task_id)).encode()).hexdigest()[:45]
    conn=store.connect()
    acquired=False
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT GET_LOCK(%s,0) AS acquired',(lock_name,))
            acquired=bool(cur.fetchone()['acquired'])
        if not acquired:return
        data=task_detail(task_id)
        if data['task']['status']!='running':return
        configured={p.name:p for p in collector.provider_configuration()};providers=[]
        for response in data['responses']:
            if response['status']!='running':continue
            p=configured[response['provider']]
            if not p.key:raise ValueError('模型密钥未配置')
            providers.append(collector.Provider(p.name,p.key,p.base_url,response['model_name']))
        task=data['task']
        prompt = task['template_text'].replace('{{question}}',task['question'])
        if task.get('knowledge_base') == 'health' and 'knowledge_points' in task['template_text']:
            from services.health_knowledge_import_service import render_prompt
            prompt = render_prompt(task['template_text'],task['question'])
        collector.execute_task(task_id,prompt,providers,
                               web_search=task['web_search'])
    finally:
        if acquired:
            with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(lock_name,))
        conn.close()
