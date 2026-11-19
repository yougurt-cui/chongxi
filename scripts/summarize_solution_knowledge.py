#!/usr/bin/env python3
"""Preview or summarize saved solution sources with Qwen; no source recollection."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store,solution_scene_service as scenes
from services.solution_knowledge_service import source_rows


def run(*,apply=False,backup_root=None):
    with store.connect() as conn,conn.cursor() as cur:
        rows=source_rows(cur)
        groups=scenes.source_groups(rows,use_alignment=True)
        cur.execute('SELECT * FROM solution_knowledge_point ORDER BY id');before=list(cur.fetchall())
    result={'status':'preview','groups':len(groups),'multi_model_groups':sum(len({(s['source_provider'],s['source_model']) for s in v})>1 for v in groups.values()),'results':[],'errors':[]}
    if not apply:return result
    root=Path(backup_root or Path(__file__).resolve().parents[1]/'var/solution-summary-runs');root.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f');backup=root/(stamp+'_before.json');report=root/(stamp+'_report.json')
    with backup.open('x') as out:
        os.chmod(backup,0o600);json.dump({'rows':before,'sources':groups_to_json(groups)},out,ensure_ascii=False,default=str)
        out.flush();os.fsync(out.fileno())
    result.update(status='running',backup=str(backup),report=str(report))
    def save():
        temp=report.with_suffix('.tmp')
        with temp.open('w') as out:os.chmod(temp,0o600);json.dump(result,out,ensure_ascii=False,default=str)
        temp.replace(report)
    save();print(json.dumps({k:result[k] for k in ('status','groups','multi_model_groups','backup','report')},ensure_ascii=False),flush=True)
    groups=scenes.source_groups(rows,align=True)
    result.update(status='running',groups=len(groups),multi_model_groups=sum(len({(s['source_provider'],s['source_model']) for s in v})>1 for v in groups.values()))
    original_keys={tuple(r[k] for k in scenes.GRAIN) for r in before}
    for key in sorted(groups):
        with store.connect() as conn:
            locked=False
            name='solution-import:'+hashlib.sha256(store.database_name().encode()).hexdigest()[:40]
            try:
                with conn.cursor() as cur:
                    cur.execute('SELECT GET_LOCK(%s,10) acquired',(name,));locked=bool(cur.fetchone()['acquired'])
                    if not locked:raise ValueError('解决方案正在入库，请稍后重试')
                changed=scenes.refresh(conn,keys={key});conn.commit()
                result['results'].append({'key':key,**changed});print('solution_summary_progress',len(result['results']),len(groups),changed['changed_count'],flush=True)
            except Exception as exc:
                conn.rollback();result['errors'].append({'key':key,'error':str(exc)});print('solution_summary_failed',key,str(exc),flush=True)
            finally:
                if locked:
                    with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(name,))
        save()
    # Retire old aliases only after every replacement summary has succeeded.
    if not result['errors']:
        with store.connect() as conn:
            name='solution-import:'+hashlib.sha256(store.database_name().encode()).hexdigest()[:40]
            locked=False
            try:
                with conn.cursor() as cur:
                    cur.execute('SELECT GET_LOCK(%s,10) acquired',(name,));locked=bool(cur.fetchone()['acquired'])
                    if not locked:raise ValueError('解决方案正在入库，请稍后重试清理旧归组')
                result['retired']=scenes.refresh(conn,keys=original_keys-set(groups))['changed_count'];conn.commit()
            except Exception as exc:
                conn.rollback();result['errors'].append({'stage':'retire_aliases','error':str(exc)})
            finally:
                if locked:
                    with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(name,))
    result['status']='partial_failed' if result['errors'] else 'completed';save()
    return result


def groups_to_json(groups):return [{'grain':key,'sources':value} for key,value in groups.items()]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');p.add_argument('--backup-root',type=Path)
    args=p.parse_args();result=run(apply=args.apply,backup_root=args.backup_root);print(json.dumps(result,ensure_ascii=False,default=str))
    if result['errors']:raise SystemExit(1)


if __name__=='__main__':main()
