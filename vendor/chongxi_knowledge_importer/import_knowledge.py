#!/usr/bin/env python3
"""Flatten five-dimensional model JSON into conditions/topic/content rows."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PROJECT_ROOT=ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))
DIMENSIONS=('applicable_object','selection_points','product_recommendations','product_content','precautions')


def to_rows(data):
    if not isinstance(data,dict) or not isinstance(data.get('dimensions'),dict):
        raise ValueError('输入必须包含 dimensions 对象')
    if set(data['dimensions'])!=set(DIMENSIONS):raise ValueError('必须包含且仅包含约定的五个维度')
    rows=[];seen=set()
    for dimension in DIMENSIONS:
        items=data['dimensions'][dimension]
        if not isinstance(items,list):raise ValueError('每个维度必须为数组')
        for item in items:
            if not isinstance(item,dict):raise ValueError('每条知识必须为对象')
            content=item.get('content');topic=item.get('topic','');conditions=item.get('applicable_conditions',[])
            structured = bool(item.get('category'))
            if structured and not content and dimension == 'product_recommendations':
                content = '；'.join(str(item[k]).strip() for k in ('brand','product_name','reason') if item.get(k))
            if structured and not content and dimension == 'product_content':
                content = json.dumps({k:v for k,v in item.items() if k not in ('category','related_categories','evidence_status','applicable_conditions')},ensure_ascii=False,sort_keys=True)
                if not item.get('product_name'): raise ValueError('产品信息缺少 product_name')
            if structured and not topic: topic = item['category']
            if structured and item.get('life_stage'):
                if not isinstance(item['life_stage'],str): raise ValueError('life_stage 必须为字符串')
                if isinstance(conditions,list): conditions = [*conditions,item['life_stage']]

            if not isinstance(content,str) or not content.strip():raise ValueError('content 不可为空')
            if not isinstance(topic,str):raise ValueError('topic 必须为字符串')
            if not isinstance(conditions,list) or any(not isinstance(c,str) for c in conditions):
                raise ValueError('applicable_conditions 必须为字符串数组')
            content=content.strip();topic=topic.strip()
            conditions=list(dict.fromkeys(c.strip() for c in conditions if c.strip()))
            if len(content)>20000 or len(topic)>255:raise ValueError('知识正文或主题过长')
            # Only exact triples within this model response are reused. Preserve full context.
            key=(tuple(sorted(conditions)),topic,content, json.dumps(item,ensure_ascii=False,sort_keys=True) if structured else '')
            if key in seen:continue
            seen.add(key)
            row={'applicable_conditions':conditions,'topic':topic,'content':content}
            if structured:
                allowed={'pet_profile','life_stage_nutrition','digestive_tolerance','food_selection','product_recommendation','product_suitability','product_overview'}
                if item['category'] not in allowed: raise ValueError('选粮知识分类无效')
                related=item.get('related_categories',[])
                if not isinstance(related,list) or any(not isinstance(c,str) or c not in allowed for c in related): raise ValueError('关联分类无效')
                row['source_context']={'question':data.get('question',''),'category':item['category'],
                    'related_categories':related,'evidence_status':item.get('evidence_status'),
                    'structured_item':item}
            rows.append(row)
    return data.get('question',''),rows


def run_import(path,dry_run=False):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if dry_run:
        question,rows=to_rows(data)
        print(json.dumps({'question':question,'count':len(rows),'rows':rows},ensure_ascii=False,indent=2));return
    from services import model_knowledge_import_service as service
    print(json.dumps(service.import_file(Path(path),data),ensure_ascii=False,default=str))


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--dry-run',action='store_true')
    args=ap.parse_args();run_import(args.input,args.dry_run)
