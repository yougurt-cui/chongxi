#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宠析健康知识结构化：选粮六维 JSON -> 健康知识卡/知识点。
仅使用 Python 标准库；兼容 OpenAI Chat Completions 风格的模型 API。
示例：python health_pipeline.py --input example_selection.json --enums '/path/to/健康枚举.xlsx' --output output
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
EVIDENCE = {'supported','uncertain','unknown','conditional','limited','insufficient'}


def read_xlsx_rows(path):
    """用 zip/XML 读取 xlsx 值，不依赖 pandas/openpyxl。"""
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.fromstring(z.read('xl/sharedStrings.xml'))
            shared = [''.join(t.text or '' for t in si.findall('.//m:t', NS))
                      for si in root.findall('m:si', NS)]
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        rel_map = {r.attrib['Id']:r.attrib['Target'] for r in rels}
        for sh in wb.findall('.//m:sheets/m:sheet', NS):
            target = rel_map[sh.attrib['{%s}id' % NS['r']]]
            filename = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            if not filename.startswith('xl/'):
                filename = 'xl/' + filename
            root = ET.fromstring(z.read(filename))
            rows = []
            for row in root.findall('.//m:sheetData/m:row', NS):
                vals = {}
                for c in row.findall('m:c', NS):
                    letters = re.match(r'[A-Z]+', c.attrib.get('r','A')).group()
                    idx = 0
                    for letter in letters: idx = idx*26 + ord(letter)-64
                    celltype = c.attrib.get('t')
                    v = c.find('m:v', NS)
                    inline = c.find('m:is', NS)
                    if celltype == 's' and v is not None:
                        value = shared[int(v.text)]
                    elif celltype == 'inlineStr' and inline is not None:
                        value = ''.join(t.text or '' for t in inline.findall('.//m:t', NS))
                    elif v is not None:
                        value = v.text or ''
                    else:
                        value = ''
                    vals[idx - 1] = value
                if vals:
                    rows.append([vals.get(i, '') for i in range(max(vals)+1)])
            yield sh.attrib['name'], rows


def load_enums(path):
    sheets = dict(read_xlsx_rows(path))
    def locate(prefix):
        matches = [x for x in sheets if prefix in x]
        if len(matches) != 1:
            raise ValueError('枚举 Excel 找不到唯一工作表: '+prefix)
        return sheets[matches[0]]
    categories = {}
    for r in locate('健康分类枚举')[1:]:
        if len(r) >= 2 and r[0]:
            categories[str(r[0]).strip()] = {'name':str(r[1]).strip(), 'key':r[2] if len(r)>2 else ''}
    problems = {}
    for r in locate('健康问题枚举')[1:]:
        if len(r)>=3 and r[0]:
            aliases = re.split('[、,，;/；]',str(r[4]) if len(r)>4 else '')
            problems[str(r[0]).strip()] = {
                'name':str(r[1]).strip(), 'category_code':str(r[2]).strip(),
                'aliases':[x.strip() for x in aliases if x.strip()]}
    types = set()
    for r in locate('知识点类型枚举')[1:]:
        if r and r[0]: types.add(str(r[0]).strip())
    needed = {'CAUSE','MECHANISM','MANIFESTATION','CONDITION'}
    if not needed.issubset(types):
        raise ValueError('枚举中缺少知识类型: '+str(needed-types))
    return categories,problems,types


def match_problem(text, problems):
    """保守匹配：精确名称/别名，或语句含完整别名；不对模糊词强行落正式编码。"""
    text = str(text or '').strip()
    normalized = re.sub(r'[\s?？。！，,、]+','',text)
    if not normalized:
        return None
    choices={code for code,item in problems.items() for name in [code,item['name']]+item['aliases']
             if normalized == re.sub(r'[\s?？。！，,、]+','',name)}
    return next(iter(choices)) if len(choices)==1 else None


def normalize_evidence(value):
    return value if value in EVIDENCE else 'insufficient'


def list_str(value):
    if isinstance(value, list):
        return list(dict.fromkeys(x.strip() for x in value if isinstance(x,str) and x.strip()))
    return []


def llm_extract(source, problems, categories, base_url, model, api_key):
    """输入选粮六维数据，模型只抽取源 JSON 已提到的健康问题和候选因果知识。"""
    health_risks = source.get('dimensions',{}).get('health_risks',[])
    if not health_risks:
        return {'items': []}
    allowed = [{'code':k,'name':v['name'],'aliases':v['aliases'], 'category_code':v['category_code']}
               for k,v in problems.items()]
    prompt = '''你是宠析选粮结果的健康知识结构化抽取器，不是自由生成问诊知识的助手。
仅依据提供的原始JSON的 health_risks（可参考其余维度解释术语），提取其中明确涉及的健康问题、可能原因、机制、伴随表现和判断依据。
禁止凭常识扩充任何源数据没有提及的具体因果关系和检查依据。
不能从健康风险提示反推该品种一定患病。不能把「建议观察」改写为确定的伴随症状。
如果源数据没有足够信息，请使用空字符串或空数组，并标记 review_required=true。
只允许从枚举表选择能明确映射的问题编码，无法匹配时 problem_code=null，保留 problem_name。
每条来源内容最多一个主健康问题，除非确实写了多个清晰独立问题。
每一个健康问题输出一条或多条独立原因-机制条目，但不要编造填补缺失信息。
仅输出如下JSON（不能输出任何其他文字）：
{"items":[{"problem_code":null,"problem_name":"","source_topic":"","cause":"","mechanism":"","manifestations":[],"verification":{"supporting":[],"opposing":[],"required_information":[]},"evidence_status":"conditional","review_required":true}]}
证据状态只允许 supported、conditional、limited、insufficient。supported 不代表验证过来源。
'''
    body = {'source_json':source, 'health_problem_enums':allowed,
            'category_enums':[{'code':k,'name':v['name']} for k,v in categories.items()]}
    url = base_url.rstrip('/')
    if not url.endswith('/chat/completions'):
        url += '/chat/completions'  # base URL must already include the provider API version
    payload = {'model':model,'temperature':0.1,
               'messages':[{'role':'system','content':prompt},
                           {'role':'user','content':json.dumps(body,ensure_ascii=False)}]}
    req = urllib.request.Request(url, data=json.dumps(payload,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=90) as resp:
            response=json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError('模型调用 HTTP '+str(e.code)+': '+e.read().decode('utf-8','replace')[:700])
    content=response['choices'][0]['message']['content']
    if isinstance(content,list):
        content=''.join(x.get('text','') for x in content if isinstance(x,dict))
    content = content.strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*|\s*```$','',content)
    out=json.loads(content)
    if not isinstance(out,dict) or not isinstance(out.get('items'),list):
        raise ValueError('模型输出缺少 items 数组')
    return out


def is_health_format(doc):
    return isinstance(doc.get('knowledge_points'),list) and ('problem' in doc)


def source_to_items(doc, problems, categories, api_config):
    if is_health_format(doc):
        return [dict(p,problem_code=doc.get('problem_code'),problem_name=doc.get('problem'),
                     source_topic=doc.get('question','')) for p in doc['knowledge_points']]
    if 'dimensions' not in doc or not isinstance(doc['dimensions'],dict):
        raise ValueError('输入需为选粮 dimensions JSON 或健康 knowledge_points JSON')
    risks=doc['dimensions'].get('health_risks',[])
    if not isinstance(risks,list): raise ValueError('health_risks 必须是数组')
    if not risks:return []
    if not all(api_config.values()):
        raise ValueError('六维选粮 JSON 不包含完整原因/机制/判断依据，必须配置 --model、LLM_API_KEY 和 LLM_BASE_URL 进行二次结构化，不能直接凭空补齐。')
    return llm_extract(doc,problems,categories,**api_config).get('items',[])


def transform(doc, enums, api_config):
    categories,problems,types=enums
    if isinstance(doc.get('dimensions'),dict):
        try:
            from .task_parser import sources_from_dimensions, classify_sources
        except ImportError:
            from task_parser import sources_from_dimensions, classify_sources
        points=classify_sources(sources_from_dimensions(doc),doc.get('question',''),enums)
        for point in points:
            point['card_id']=point['problem_code'] or point['source_id']
            point['point_id']=point['source_id']
        return {'meta':{'source_format':'health_dimensions','point_count':len(points),'note':'原文保留，候选知识待审核'},
                'cards':list({(p['problem_code'] or p['source_id']): {'card_id':p['problem_code'] or p['source_id'],
                    'category_code':p['category_code'],'problem_code':p['problem_code'],
                    'problem_name':problems.get(p['problem_code'],{}).get('name',p['source_topic']),
                    'status':'pending_review'} for p in points}.values()), 'knowledge_points':points,
                'review_queue':[{'source_index':p['source_id'],'problem_name':p['source_topic'],'reason':'；'.join(p['review_reasons'])} for p in points]}
    items=source_to_items(doc,problems,categories,api_config)
    cards={}
    points=[]
    review=[]
    for i,x in enumerate(items,1):
        if not isinstance(x,dict):continue
        raw_code=x.get('problem_code')
        raw_name=str(x.get('problem_name') or doc.get('problem') or '').strip()
        code = raw_code if raw_code in problems else match_problem(raw_name,problems)
        # direct health format could supply coarse category; never use it to override problem enum
        if not code:
            review.append({'source_index':i,'problem_name':raw_name,'reason':'健康问题未匹配42项枚举，需要审核'})
        problem = problems.get(code,{})
        cat_code=problem.get('category_code')
        card_id=code or ('PENDING_'+str(i).zfill(3))
        if card_id not in cards:
            cards[card_id]={
                'card_id':card_id,'category_code':cat_code,'category_name':categories.get(cat_code,{}).get('name'),
                'problem_code':code,'problem_name':problem.get('name') or raw_name,
                'source_question':doc.get('question',''),'status':'mapped' if code else 'pending_mapping'}
        v=x.get('verification') or {}
        if not isinstance(v,dict):v={}
        supporting=list_str(v.get('supporting'))
        opposing=list_str(v.get('opposing'))
        required=list_str(v.get('required_information'))
        cause=str(x.get('cause') or '').strip()
        mechanism=str(x.get('mechanism') or '').strip()
        manifests=list_str(x.get('manifestations'))
        # allow empty fields but flag for review
        incomplete=not (cause and mechanism and (supporting or opposing or required))
        review_flag=bool(x.get('review_required')) or incomplete or not code
        if review_flag:
            review.append({'source_index':i,'problem_name':raw_name,'reason':'知识要素不全/模型要求复核/枚举未映射'})
        base={'card_id':card_id, 'source_index':i,'evidence_status':normalize_evidence(x.get('evidence_status')),
              'source_topic':x.get('source_topic',''),'review_required':review_flag}
        # 保留每条因果链的 group_id，避免导出原子知识点时丢失关联
        group_id=f'{card_id}_G{i:03d}'
        for t,contents in [('CAUSE',[cause] if cause else []),('MECHANISM',[mechanism] if mechanism else []),
                           ('MANIFESTATION',manifests),
                           ('CONDITION',['支持：'+s for s in supporting]+['削弱：'+s for s in opposing]+
                            ['待补充：'+s for s in required])]:
            for content in contents:
                points.append({**base,'point_id':f'KP{len(points)+1:05d}','group_id':group_id,
                               'knowledge_type':t,'content':content})
    return {'meta':{'source_question':doc.get('question'),
                    'generated_at':datetime.now(timezone.utc).isoformat(),
                    'source_format':'health' if is_health_format(doc) else 'selection',
                    'category_count':len(categories),'problem_enum_count':len(problems),
                    'card_count':len(cards),'point_count':len(points),
                    'note':'候选知识，映射不等于医学证据已核验'},
            'cards':list(cards.values()),'knowledge_points':points,'review_queue':review}


def csv_write(path,rows,columns):
    with open(path,'w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');w.writeheader()
        w.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in row.items()} for row in rows)


def main():
    ap=argparse.ArgumentParser(description='宠析健康知识结构化流水线')
    ap.add_argument('--input',required=True,help='原始模型输出JSON文件')
    ap.add_argument('--enums',required=True,help='宠析健康知识库分类枚举xlsx')
    ap.add_argument('--output',default='./health_output',help='输出目录')
    ap.add_argument('--model',default=os.getenv('LLM_MODEL',''),help='兼容Chat Completions接口的模型名')
    ap.add_argument('--base-url',default=os.getenv('LLM_BASE_URL',''),help='例如https://api.example.com/v1')
    args=ap.parse_args()
    with open(args.input,'r',encoding='utf-8-sig') as f: doc=json.load(f)
    enums=load_enums(args.enums)
    api={'base_url':args.base_url,'model':args.model,'api_key':os.getenv('LLM_API_KEY','')}
    result=transform(doc,enums,api)
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    (out/'health_structured.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    csv_write(out/'health_cards.csv',result['cards'],
              ['card_id','category_code','category_name','problem_code','problem_name','source_question','status'])
    csv_write(out/'health_points.csv',result['knowledge_points'],
              ['point_id','card_id','group_id','source_id','source_dimension','source_index','category_code','problem_code','knowledge_type','content','evidence_status','source_topic','applicable_conditions','source_urls','review_status','review_reasons','review_required'])
    csv_write(out/'review_queue.csv',result['review_queue'],['source_index','problem_name','reason'])
    print(json.dumps(result['meta'],ensure_ascii=False,indent=2))
    print('输出目录：',out.resolve())

if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError,KeyError,FileNotFoundError,json.JSONDecodeError) as e:
        print('ERROR:',str(e),file=sys.stderr);sys.exit(1)
