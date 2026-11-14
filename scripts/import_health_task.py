#!/usr/bin/env python3
"""Import successful health batches of a multi-model task using the live dictionaries."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id',type=int,required=True)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id,knowledge_base,status,parsed_response FROM knowledge_import_batch WHERE task_id=%s ORDER BY id',(args.task_id,))
            rows=list(cur.fetchall())
    if not rows:raise ValueError('任务不存在')
    if any(r['knowledge_base']!='health' for r in rows):raise ValueError('只能解析健康知识库任务')
    results=[]
    for r in rows:
        if r['status']!='success':continue
        if args.dry_run:
            from vendor.chongxi_health_pipeline.task_parser import sources_from_dimensions
            results.append({'batch_id':r['id'],'source_count':len(sources_from_dimensions(store.decode(r['parsed_response'])))})
        else:results.append(store.import_batch(r['id']))
    print(json.dumps({'task_id':args.task_id,'dry_run':args.dry_run,'results':results},ensure_ascii=False))


if __name__=='__main__':main()
