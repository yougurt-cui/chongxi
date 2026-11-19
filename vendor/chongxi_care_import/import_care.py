#!/usr/bin/env python3
"""Import Chongxi care knowledge JSON into a single MySQL wide table.

Default mode is CONSERVATIVE: one source care_activity -> one row with unknown
life stage/subtype unless the top-level scope explicitly specifies otherwise.
No fabricated kitten/adult/internal/external records.

Optional --llm-partition uses an OpenAI-compatible Chat Completions endpoint to
extract separate applicable knowledge rows; each returned fact is checked against
input text. This is still not medical validation; rows remain pending_review.

Dry-run is default. Add --commit to write to MySQL.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

JSON_COLUMNS = ('applicable_conditions', 'schedule', 'tools_materials', 'methods',
                'avoidances', 'observations', 'adjustment_rules', 'source_ids',
                'extraction_notes', 'original_payload', 'source_scope')
COLUMNS = ('knowledge_key','species','breed','life_stage','need_code','care_activity_code',
           'care_activity_name','care_subtype','care_domain','care_category','care_type',
           'applicable_conditions','schedule','tools_materials','methods','avoidances',
           'observations','adjustment_rules','source_question','source_ids','evidence_status',
           'review_status','source_batch','extraction_mode','extraction_notes','original_payload','source_scope')
ALLOWED_STAGE={'neonatal','weaning','kitten','puppy','adult','senior','adult_dog','senior_dog','all','all_stages','unknown','unspecified'}
ALLOWED_SUBTYPE={'internal','external','both','all','unknown'}

# Code mapping is customizable: no guessing health-condition names as needs.
NEED_CODES={'寄生虫预防':'parasite_prevention'}


def validate_root(root):
    if not isinstance(root,dict) or not isinstance(root.get('care_activities'),list) or not 1 <= len(root['care_activities']) <= 100:
        raise ValueError('Root object must contain 1–100 care_activities')
    json.dumps(root,allow_nan=False)
    if not isinstance(root.get('question'),str) or not root['question'].strip():
        raise ValueError('question must be a non-empty string')
    for activity in root['care_activities']:
        if not isinstance(activity,dict):raise ValueError('care_activity must be an object')
        for name,limit in [('care_activity_code',100),('care_activity_name',150)]:
            value=activity.get(name)
            if not isinstance(value,str) or not value.strip() or len(value)>limit:
                raise ValueError(name+' is required or too long')
        for name,limit in [('species',32),('breed',100),('life_stage',32),('need_code',100),('care_subtype',64),('care_domain',64),('care_category',100),('care_type',32)]:
            value=activity.get(name,root.get(name))
            if value is not None and (not isinstance(value,str) or len(value)>limit):raise ValueError(name+' must be a string within '+str(limit)+' characters')
        types={'applicable_conditions':(dict,list),'schedule':(dict,),
               'tools_materials':(list,),'methods':(list,),'avoidances':(list,),
               'observations':(dict,),'adjustment_rules':(list,),'source_ids':(list,)}
        for name,allowed in types.items():
            value=activity.get(name)
            if value is not None and not isinstance(value,allowed):raise ValueError(name+' has an invalid JSON type')
        for phase,values in (activity.get('observations') or {}).items():
            if not isinstance(values,list):raise ValueError('observations.'+phase+' must be an array')
        knowledge=[v for k in ('schedule','tools_materials','methods','avoidances','observations','adjustment_rules') for v in leaf_values(activity.get(k))]
        if not any((isinstance(v,str) and v.strip()) or type(v) in (int,float,bool) for v in knowledge):
            raise ValueError('care_activity has no knowledge content')
    return root


def decode_json(content):
    content=content.lstrip('\ufeff').strip()
    if content.startswith('```'):
        content=re.sub(r'^```(?:json)?\s*','',content,flags=re.I)
        content=re.sub(r'\s*```$','',content)
    return json.loads(content)


def read_input(path):
    root=decode_json(Path(path).read_text(encoding='utf-8-sig'))
    if isinstance(root,dict) and not root.get('care_activities'):
        raw=root.get('raw_response') or root.get('raw_text')
        if isinstance(raw,str):root=decode_json(raw)
    return validate_root(root)


def scope_value(root,activity,key):
    value=activity.get(key)
    if value is None or value=='':value=root.get(key)
    return str(value).strip() if value is not None and str(value).strip() else 'unknown'


def normalized_scope(value, allowed):
    value=str(value or 'unknown').strip().lower()
    return value if value in allowed else 'unknown'


def infer_need(activity):
    return activity.get('need_code') or NEED_CODES.get(activity.get('care_category')) or NEED_CODES.get(activity.get('care_domain')) or 'unknown'


def source_strings(value):
    if isinstance(value,str):
        return [value]
    if isinstance(value,dict):
        return [x for v in value.values() for x in source_strings(v)]
    if isinstance(value,list):
        return [x for v in value for x in source_strings(v)]
    return []


def sanitize_activity_parts(activity):
    return {k: activity.get(k) for k in ('applicable_conditions','schedule','tools_materials',
                       'methods','avoidances','observations','adjustment_rules')}


def make_base(root,activity,batch):
    return {'species':scope_value(root,activity,'species'),
            'breed':scope_value(root,activity,'breed'),
            'life_stage':scope_value(root,activity,'life_stage'),
            'need_code':infer_need(activity),
            'care_activity_code':activity['care_activity_code'].strip(),
            'care_activity_name':activity['care_activity_name'].strip(),
            'care_subtype':scope_value(root,activity,'care_subtype'),
            'care_domain':activity.get('care_domain'),
            'care_category':activity.get('care_category'),
            'care_type':activity.get('care_type'),
            'source_question':root.get('question'),
            'source_ids':activity.get('source_ids') or [],
            'evidence_status':activity.get('evidence_status') or 'pending_review',
            'review_status':'pending_review',
            'source_batch':batch,
            'original_payload':activity,
            'source_scope':{k:root.get(k) for k in ('species','breed','life_stage','care_subtype')}}


def make_key(record):
    identity=[record[x] for x in ('source_batch','species','breed','life_stage','need_code',
                'care_activity_code','care_subtype')]
    identity += [record['original_payload'],record['source_scope'],sanitize_activity_parts(record)]
    return hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def conservative(root,activity,batch):
    row=make_base(root,activity,batch)
    row.update(sanitize_activity_parts(activity))
    row['extraction_mode']='conservative'
    row['extraction_notes']=[
        '一条原始养护活动对应一条候选记录；优先保留活动内明确维度，未指定时继承顶层',
        '不通过用药途径推断体内/体外，不扩增缺少依据的生命阶段或场景',
        '原始内容完整保留；周期、剂量、来源仍需人工审核']
    row['knowledge_key']=make_key(row)
    return [row]

SYSTEM_PROMPT='''你是宠析养护知识的受限提取器，只进行原文分类，不创造新医学事实。
目标：从一条原始养护活动中拆分成有明确适用范围的宽表记录。
输出严格 JSON: {"rows":[{"life_stage":"原文已有阶段编码或unknown", "care_subtype":"原文已有子类型编码或unknown", "applicable_conditions":[], "schedule":null, "tools_materials":[], "methods":[], "avoidances":[], "observations":{"before":[],"during":[],"after":[]}, "adjustment_rules":[], "notes":[]}]}。
必须遵循：
1. 只能从输入中逐字提取已有项目、列表元素、字段；不得改写、概括、填充新医学事实或修正医学建议。
2. 应用条件无明确年龄或类型时设为 unknown，不能设为 all；明确跨全部阶段的通用安全规则可以 all。
3. 体内/体外对应目标寄生虫，不等于口服/外用给药途径；不明则 unknown。
4. 各场景不要复制不适用的项目；区分不了归属的项目单独放在 unknown 记录中。
5. 对原文中存在可疑药物剂量、固定频率和无来源医学阈值写入 notes，标记待审核；绝不把这些内容认定为已核实。
6. 适用范围相同的材料合并到一行；不要为了凑幼猫/成猫四组合创造空记录。
7. applicable_conditions保持原字段的对象或数组类型；不改变原文明确的阶段或子类型。
8. 只返回JSON。'''


def literal_subset(candidate,original):
    if candidate is None:return True
    if isinstance(candidate,dict):
        return isinstance(original,dict) and all(k in original and literal_subset(v,original[k]) for k,v in candidate.items())
    if isinstance(candidate,list):
        return isinstance(original,list) and all(any(literal_subset(v,old) for old in original) for v in candidate)
    return type(candidate) is type(original) and candidate==original


def llm_partition(root,activity,batch,model,base_url,api_key):
    if not api_key: raise ValueError('Set OPENAI_API_KEY or --api-key for --llm-partition')
    url=base_url.rstrip('/') + '/chat/completions'
    payload={'model':model,'temperature':0,
             'messages':[{'role':'system','content':SYSTEM_PROMPT},
                         {'role':'user','content':json.dumps({'root_scope':{k:root.get(k) for k in ('question','species','breed','life_stage')},'activity':activity},ensure_ascii=False)}],
             'response_format':{'type':'json_object'}}
    req=Request(url,data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
                headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},method='POST')
    with urlopen(req,timeout=90) as response:
        response_data=json.load(response)
    content=response_data['choices'][0]['message']['content']
    result=json.loads(content)
    if not isinstance(result.get('rows'),list) or not result['rows']: raise ValueError('LLM response lacks non-empty rows array')
    # Validate extracted leaf facts. We also preserve original source in original_payload.
    rows=[]
    for i,part in enumerate(result['rows']):
        if not isinstance(part,dict): raise ValueError('Bad row')
        row=make_base(root,activity,batch)
        for field,allowed in (('life_stage',ALLOWED_STAGE),('care_subtype',ALLOWED_SUBTYPE)):
            value=part.get(field) or row[field]
            if not isinstance(value,str):raise ValueError('LLM scope must be a string')
            if row[field] not in {'unknown','unspecified'} and value!=row[field]:
                raise ValueError('LLM changed explicit source scope: '+field)
            if value not in allowed and value!=row[field]:raise ValueError('LLM invented unsupported scope: '+field)
            if row[field] in {'unknown','unspecified'} and value not in {'unknown','unspecified'}:
                evidence=' '.join(source_strings(part.get('applicable_conditions'))).casefold()
                labels={'kitten':('幼猫','kitten'),'puppy':('幼犬','puppy'),
                        'adult':('成年','成猫','成犬','adult'),'senior':('老年','senior'),
                        'neonatal':('新生','neonatal'),'weaning':('断奶','weaning'),
                        'all':('全生命阶段','所有生命阶段','全部类型','all stages'),
                        'all_stages':('全生命阶段','所有生命阶段','all stages'),
                        'internal':('体内寄生虫','肠道寄生虫','internal'),
                        'external':('体外寄生虫','external'),'both':('体内和体外','体内外')}
                if not any(label in evidence for label in labels.get(value,(value,))):
                    raise ValueError('LLM scope lacks explicit applicable-condition evidence: '+field)
            row[field]=value

        row['applicable_conditions']=part.get('applicable_conditions')
        for field in ('schedule','tools_materials','methods','avoidances','observations','adjustment_rules'):
            row[field]=part.get(field)
        for field in ('applicable_conditions','schedule','tools_materials','methods','avoidances','observations','adjustment_rules'):
            if not literal_subset(row[field],activity.get(field)):
                raise ValueError(f'LLM added/rewrote a fact in row {i} field {field}')
        row['extraction_mode']='llm_partition'
        row['extraction_notes']=part.get('notes') or []
        row['knowledge_key']=make_key(row)
        rows.append(row)
    if len({(r['life_stage'],r['care_subtype']) for r in rows})!=len(rows):
        raise ValueError('Duplicate context rows from LLM: merge same scope instead')
    for field in ('applicable_conditions','schedule','tools_materials','methods','avoidances','observations','adjustment_rules'):
        original_values=set(json.dumps(v,ensure_ascii=False,sort_keys=True) for v in leaf_values(activity.get(field)))
        extracted=set(json.dumps(v,ensure_ascii=False,sort_keys=True) for row in rows for v in leaf_values(row[field]))
        if not original_values.issubset(extracted):raise ValueError('LLM dropped source content in '+field)
    return rows


def leaf_values(value):
    if isinstance(value,dict):return [leaf for v in value.values() for leaf in leaf_values(v)]
    if isinstance(value,list):return [leaf for v in value for leaf in leaf_values(v)]
    return [] if value is None else [value]


def parse_document(root,batch='default'):
    validate_root(root)
    if not isinstance(batch,str) or not 1<=len(batch)<=100:raise ValueError('source batch must be 1–100 characters')
    rows=[];seen=set()
    for activity in root['care_activities']:
        for row in conservative(root,activity,batch):
            if row['knowledge_key'] not in seen:rows.append(row);seen.add(row['knowledge_key'])
    return rows


def write_json(rows,path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    Path(path).write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')


def import_mysql(rows,host,port,user,password,database):
    try: import pymysql
    except ImportError as exc: raise RuntimeError('pip install PyMySQL') from exc
    con=pymysql.connect(host=host,port=port,user=user,password=password,database=database,
                        charset='utf8mb4',autocommit=False,connect_timeout=10)
    colsql=', '.join('`'+x+'`' for x in COLUMNS)
    placeholders=', '.join(['%s']*len(COLUMNS))
    # Immutable source versions: retries never reset a human review or bump version.
    sql=f'INSERT INTO pet_care_knowledge ({colsql}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE knowledge_key=knowledge_key'
    try:
        with con.cursor() as cur:
            cur.execute(Path(__file__).with_name('schema.sql').read_text().strip().rstrip(';'))
            cur.execute("SHOW COLUMNS FROM pet_care_knowledge LIKE 'source_scope'")
            if not cur.fetchone():cur.execute('ALTER TABLE pet_care_knowledge ADD COLUMN source_scope JSON NULL')
            for row in rows:
                args=[json.dumps(row.get(c),ensure_ascii=False) if c in JSON_COLUMNS and row.get(c) is not None else row.get(c) for c in COLUMNS]
                cur.execute(sql,args)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input')
    p.add_argument('--project-root',type=Path,help='Existing Chongxi backend checkout; required for stored model batches')
    p.add_argument('--batch-id',type=int)
    p.add_argument('--task-id',type=int)
    p.add_argument('--output',default='parsed_rows.json')
    p.add_argument('--batch',default='default',help='Stable ID for one source batch; rerunning updates same rows')
    p.add_argument('--llm-partition',action='store_true')
    p.add_argument('--model',default=os.getenv('LLM_MODEL',''))
    p.add_argument('--base-url',default=os.getenv('LLM_BASE_URL','https://api.openai.com/v1'))
    p.add_argument('--api-key',default=os.getenv('OPENAI_API_KEY',''))
    p.add_argument('--commit',action='store_true',help='Actually write to database (otherwise dry-run)')
    p.add_argument('--db-host',default=os.getenv('MYSQL_HOST','127.0.0.1'))
    p.add_argument('--db-port',type=int,default=int(os.getenv('MYSQL_PORT','3306')))
    p.add_argument('--db-user',default=os.getenv('MYSQL_USER','root'))
    p.add_argument('--db-password',default=os.getenv('MYSQL_PASSWORD',''))
    p.add_argument('--db-name',default=os.getenv('MODEL_KNOWLEDGE_DATABASE','chongxi_model_knowledge'))
    args=p.parse_args()
    if args.batch_id is not None or args.task_id is not None:
        if args.input or args.llm_partition:p.error('Stored-batch mode does not accept --input or --llm-partition')
        if not args.project_root or not (args.project_root/'services/daily_care_knowledge_service.py').exists():
            p.error('--project-root must point to the updated Chongxi backend')
        sys.path.insert(0,str(args.project_root.resolve()))
        from scripts.parse_daily_care_activities import migrate
        result=migrate(apply=args.commit,task_id=args.task_id,batch_id=args.batch_id)
        write_json(result,args.output)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        if result['errors']:raise SystemExit(1)
        return
    if args.project_root:p.error('--project-root requires --task-id or --batch-id')
    if not 1<=len(args.batch)<=100:p.error('--batch must be 1–100 characters')
    if not args.input:p.error('--input or --batch-id/--task-id is required')
    root=read_input(args.input)
    rows=[]
    for act in root['care_activities']:
        if args.llm_partition:
            if not args.model: p.error('--model required for --llm-partition')
            rows.extend(llm_partition(root,act,args.batch,args.model,args.base_url,args.api_key))
        else:
            rows.extend(conservative(root,act,args.batch))
    rows=list({row['knowledge_key']:row for row in rows}.values())
    write_json(rows,args.output)
    print(f'Produced {len(rows)} pending-review rows in {args.output}')
    if args.commit:
        import_mysql(rows,args.db_host,args.db_port,args.db_user,args.db_password,args.db_name)
        print(f'Upserted {len(rows)} row(s) into {args.db_name}.pet_care_knowledge')
    else:
        print('Dry run only. Use --commit to write to MySQL.')

if __name__=='__main__':
    try: main()
    except Exception as e:
        print(f'ERROR: {e}',file=sys.stderr)
        sys.exit(1)
