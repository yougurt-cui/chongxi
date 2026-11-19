#!/usr/bin/env python3
"""Move health parse state into collection batches, verify, back up, drop old table.

Preview by default. Stop API/collection workers before --apply. Never calls a model.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store
from services.health_knowledge_import_service import ensure_parse_column


def migrate(*,apply=False,backup_root=None):
    with store.connect() as conn:
        locks=[]
        try:
            with conn.cursor() as cur:
                cur.execute('SHOW TABLES LIKE %s',('health_knowledge_import',))
                if not cur.fetchone():return {'status':'already_removed','model_calls':0}
                cur.execute("SELECT COUNT(*) n FROM knowledge_import_batch WHERE knowledge_base='health' AND status='running'")
                if cur.fetchone()['n']:raise ValueError('健康采集仍在执行，请完成后再迁移')
                cur.execute('SELECT * FROM health_knowledge_import ORDER BY batch_id');rows=list(cur.fetchall())
                ids=[r['batch_id'] for r in rows]
                if apply:
                    for batch_id in ids:
                        name='chongxi:health-import:'+str(batch_id)
                        cur.execute('SELECT GET_LOCK(%s,10) acquired',(name,))
                        if not cur.fetchone()['acquired']:raise ValueError('健康解析仍在执行，请稍后重试')
                        locks.append(name)
                    # Refresh after locks: a parser may have committed while locks were acquired.
                    cur.execute('SELECT * FROM health_knowledge_import ORDER BY batch_id');rows=list(cur.fetchall())
                    if [r['batch_id'] for r in rows]!=ids:raise ValueError('迁移期间批次发生变化，请停止采集后重试')
                batches=[]
                for batch_id in ids:
                    cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s',(batch_id,));batch=cur.fetchone()
                    if not batch or batch['knowledge_base']!='health':raise ValueError('解析状态存在缺失/非健康批次：'+str(batch_id))
                    batches.append(batch)
                states=[]
                for row,batch in zip(rows,batches):
                    state={k:v for k,v in row.items() if k!='batch_id'}
                    state['source_exclusions']=store.decode(state.get('source_exclusions'))
                    state=json.loads(json.dumps(state,default=str))
                    previous=store.decode(batch.get('health_parse_result'))
                    if previous is not None and previous!=state:raise ValueError('批次已有不同的解析状态，拒绝覆盖：'+str(batch['id']))
                    states.append(state)
                result={'status':'preview','rows':len(rows),'target':'knowledge_import_batch.health_parse_result','model_calls':0}
                if not apply:return result
                cur.execute('SHOW CREATE TABLE health_knowledge_import');schema=cur.fetchone()
                root=Path(backup_root or Path(__file__).resolve().parents[1]/'var/health-import-backups');root.mkdir(parents=True,exist_ok=True)
                backup=root/('before_remove_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.json')
                with backup.open('x',encoding='utf-8') as out:
                    os.chmod(backup,0o600)
                    json.dump({'schema':schema,'rows':rows,'batches':batches},out,ensure_ascii=False,default=str)
                    out.flush();os.fsync(out.fileno())
                ensure_parse_column(cur)
                for row,state in zip(rows,states):
                    cur.execute('UPDATE knowledge_import_batch SET health_parse_result=%s WHERE id=%s',(store.dumps(state),row['batch_id']))
                conn.commit()
                for row,state in zip(rows,states):
                    cur.execute('SELECT health_parse_result FROM knowledge_import_batch WHERE id=%s',(row['batch_id'],))
                    if store.decode(cur.fetchone()['health_parse_result'])!=state:raise ValueError('迁移校验失败，旧表保留')
                cur.execute('SELECT COUNT(*) n FROM health_knowledge_import')
                if cur.fetchone()['n']!=len(rows):raise ValueError('旧表迁移期间发生变化，拒绝删除')
                cur.execute('DROP TABLE health_knowledge_import')
                conn.commit()
                return {**result,'status':'completed','backup':str(backup),'dropped_table':'health_knowledge_import'}
        except Exception:
            conn.rollback();raise
        finally:
            for name in reversed(locks):
                with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(name,))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--backup-root',type=Path)
    args=parser.parse_args();print(json.dumps(migrate(apply=args.apply,backup_root=args.backup_root),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
