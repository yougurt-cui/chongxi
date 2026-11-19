#!/usr/bin/env python3
"""Preview/reparse saved daily-care responses, without calling a model."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store


def migrate(*,apply=False,task_id=None,batch_id=None,backup_root=None):
    clauses=["knowledge_base='daily_care'","status='success'"];params=[]
    for name,value in (('task_id',task_id),('id',batch_id)):
        if value is not None:
            if type(value) is not int or value<1:raise ValueError('任务/批次ID必须是正整数')
            clauses.append(name+'=%s');params.append(value)
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT * FROM knowledge_import_batch WHERE '+' AND '.join(clauses)+' ORDER BY id',params)
        batches=list(cur.fetchall());details=[];schemas={}
        if batches:
            ids=[b['id'] for b in batches]
            cur.execute('SELECT * FROM daily_care_knowledge_point_detail WHERE batch_id IN ('+','.join(['%s']*len(ids))+') ORDER BY id',ids);details=list(cur.fetchall())
        if apply:
            for table in ('knowledge_import_batch','daily_care_knowledge_point_detail'):
                cur.execute('SHOW CREATE TABLE '+table);schemas[table]=cur.fetchone()
    if (task_id is not None or batch_id is not None) and not batches:
        raise LookupError('没有找到对应的、采集成功的日常养护批次')
    from vendor.chongxi_multi_model_mysql.run import extract_json
    from services.daily_care_activity_adapter import points_for
    results=[];errors=[]
    for batch in batches:
        try:
            doc=extract_json(batch['raw_response'] or '',knowledge_base='daily_care',question=batch['question'])
            points=points_for(doc) if 'care_activities' in doc else doc['care_points']
            results.append({'batch_id':batch['id'],'task_id':batch['task_id'],'provider':batch['provider'],
                            'format':'care_activities' if 'care_activities' in doc else 'care_points','point_count':len(points)})
        except Exception as exc:errors.append({'batch_id':batch['id'],'error':str(exc)})
    summary={'mode':'apply' if apply else 'preview','database':store.database_name(),
             'knowledge_table':'daily_care_knowledge_point_detail','batch_count':len(batches),
             'existing_points':len(details),'model_calls':0,'results':results,'errors':errors}
    if not apply:return summary
    root=Path(backup_root or Path(__file__).resolve().parents[1]/'var/daily-care-backups');root.mkdir(parents=True,exist_ok=True)
    backup=root/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.json')
    with backup.open('x',encoding='utf-8') as output:
        json.dump({'schemas':schemas,'batches':batches,'details':details},output,ensure_ascii=False,default=str)
    store.ensure_database()
    imported=[]
    for result in results:
        try:imported.append(store.process_batch_pipeline(result['batch_id']))
        except Exception as exc:errors.append({'batch_id':result['batch_id'],'error':str(exc)})
    return {**summary,'backup':str(backup),'imported':imported,'status':'partial_failed' if errors else 'completed'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true');p.add_argument('--task-id',type=int);p.add_argument('--batch-id',type=int)
    p.add_argument('--backup-root',type=Path)
    args=p.parse_args();result=migrate(apply=args.apply,task_id=args.task_id,batch_id=args.batch_id,backup_root=args.backup_root)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    if result['errors']:raise SystemExit(1)


if __name__=='__main__':main()
