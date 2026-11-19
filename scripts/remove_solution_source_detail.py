#!/usr/bin/env python3
"""Back up and remove solution source detail table after moving snapshots to batches.

Stop API and collection workers before --apply. Preview is read-only, no model calls.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store
from services import solution_knowledge_service as solution

TABLE='solution_knowledge_point_detail'
JSON_FIELDS=(*solution.JSON_FIELDS,'raw_json')


def normalized(row):
    value=dict(row)
    for key in JSON_FIELDS:
        if key in value:value[key]=store.decode(value[key])
    return json.loads(json.dumps(value,default=str))


def migrate(*,apply=False,backup_root=None):
    with store.connect() as conn:
        lock=None
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s',(TABLE,))
                if not cur.fetchone()['n']:return {'status':'already_removed','model_calls':0}
                if apply:
                    lock='solution-import:'+hashlib.sha256(store.database_name().encode()).hexdigest()[:40]
                    cur.execute('SELECT GET_LOCK(%s,10) acquired',(lock,))
                    if not cur.fetchone()['acquired']:raise ValueError('解决方案正在解析，请稍后重试')
                cur.execute("SELECT * FROM knowledge_import_batch WHERE knowledge_base='solution' ORDER BY id")
                batches=list(cur.fetchall());byid={b['id']:b for b in batches}
                if any(b['status']=='running' for b in batches):raise ValueError('仍有解决方案采集运行中，请停止后迁移')
                cur.execute('SELECT * FROM '+TABLE+' ORDER BY id');rows=list(cur.fetchall())
                grouped={bid:[] for bid in byid}
                for row in rows:
                    if row['batch_id'] not in byid:raise ValueError('明细存在缺失或非解决方案批次，拒绝移除')
                    grouped[row['batch_id']].append(normalized(row))
                states={}
                for batch in batches:
                    state={'version':1,'points':grouped[batch['id']],'point_count':len(grouped[batch['id']]),'migration':'remove_solution_source_detail_v1'}
                    previous=store.decode(batch.get('solution_parse_result'))
                    if previous is not None and previous!=state:raise ValueError('已有不同解析快照，拒绝覆盖：'+str(batch['id']))
                    states[batch['id']]=state
                result={'status':'preview','source_rows':len(rows),'batches':len(batches),
                        'target':'knowledge_import_batch.solution_parse_result','model_calls':0}
                if not apply:return result
                snapshot={}
                for table in (TABLE,'solution_knowledge_point','health_solution'):
                    cur.execute('SHOW CREATE TABLE '+table);schema=cur.fetchone()['Create Table']
                    cur.execute('SELECT * FROM '+table+' ORDER BY id');snapshot[table]={'schema':schema,'rows':list(cur.fetchall())}
                root=Path(backup_root or Path(__file__).resolve().parents[1]/'var/solution-source-backups')
                root.mkdir(parents=True,exist_ok=True)
                backup=root/('before_remove_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.json')
                with backup.open('x',encoding='utf-8') as out:
                    os.chmod(backup,0o600);json.dump({'tables':snapshot,'batches':batches},out,ensure_ascii=False,default=str)
                    out.flush();os.fsync(out.fileno())
                solution.ensure_parse_column(cur)
                for bid,state in states.items():
                    cur.execute('UPDATE knowledge_import_batch SET solution_parse_result=%s WHERE id=%s',(store.dumps(state),bid))
                readback=solution.source_rows(cur)
                # Only successful batches participate in aggregates. All rows are still archived in batches.
                expected=[r for r in rows if byid[r['batch_id']]['status']=='success']
                stripped=[normalized({k:r[k] for k in expected[0]}) for r in readback] if expected else []
                if stripped!=[normalized(r) for r in expected]:raise ValueError('来源逐字段校验失败，旧表保留')
                from services.solution_scene_service import refresh
                from services.health_solution_service import refresh_groups,scenario_key
                refresh(conn)
                affected={scenario_key(store.decode(r['scenario_data'])) for r in expected if r.get('scenario_data')}
                refresh_groups(conn,affected)
                for table in ('solution_knowledge_point','health_solution'):
                    cur.execute('SELECT * FROM '+table+' ORDER BY id')
                    if list(cur.fetchall())!=snapshot[table]['rows']:raise ValueError('迁移导致已有场景结果变化，拒绝移除旧表')
                for bid,state in states.items():
                    cur.execute('SELECT solution_parse_result FROM knowledge_import_batch WHERE id=%s',(bid,))
                    if store.decode(cur.fetchone()['solution_parse_result'])!=state:raise ValueError('批次快照校验失败')
                conn.commit()
                cur.execute('DROP TABLE '+TABLE);conn.commit()
                return {**result,'status':'completed','backup':str(backup),'dropped_table':TABLE,'scene_results_unchanged':True}
        except Exception:
            conn.rollback();raise
        finally:
            if lock:
                with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');p.add_argument('--backup-root',type=Path)
    args=p.parse_args();print(json.dumps(migrate(apply=args.apply,backup_root=args.backup_root),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
