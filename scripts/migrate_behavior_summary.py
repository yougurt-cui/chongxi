#!/usr/bin/env python3
"""Back up and move behavior call/parse history into batches, then drop old table."""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store
from services.behavior_model_pipeline import ensure_schema


def migrate(apply=False):
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE 'behavior_model_call'")
        if not cur.fetchone():return {'status':'already_removed'}
        cur.execute("SELECT COUNT(*) n FROM knowledge_import_batch WHERE knowledge_base='behavior' AND status='running'")
        if cur.fetchone()['n']:raise ValueError('行为采集仍在运行，先停止采集再迁移')
        cur.execute('SELECT * FROM behavior_model_call ORDER BY id');calls=list(cur.fetchall())
        cur.execute("SELECT * FROM knowledge_import_batch WHERE knowledge_base='behavior' ORDER BY id");batches=list(cur.fetchall());by_id={r['id']:r for r in batches}
        for call in calls:
            batch=by_id.get(call['batch_id'])
            if not batch or batch['raw_response']!=call['raw_response']:raise ValueError('调用归档与采集原文不一致，拒绝删除')
            if batch.get('behavior_parse_result'):raise ValueError('批次已有解析状态，拒绝覆盖')
        cur.execute('SELECT * FROM behavior_knowledge_point_detail ORDER BY id');details=list(cur.fetchall())
        if not apply:return {'status':'preview','calls':len(calls),'details':len(details),'batches':len(batches)}
        cur.execute('SHOW CREATE TABLE behavior_model_call');schema=cur.fetchone()
        root=Path(__file__).resolve().parents[1]/'var/behavior-backups';root.mkdir(parents=True,exist_ok=True)
        path=root/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.json')
        with path.open('x') as out:
            os.chmod(path,0o600);json.dump({'schema':schema,'calls':calls,'batches':batches,'details':details},out,ensure_ascii=False,default=str)
        ensure_schema(cur)
        for call in calls:
            state={k:v for k,v in call.items() if k.startswith('parse_') or k in ('parser_version','point_count','parsed_at')}
            state['legacy_call']=call
            state=json.loads(json.dumps(state,default=str))
            state['parse_history']=store.decode(state.get('parse_history')) or []
            cur.execute('UPDATE knowledge_import_batch SET behavior_parse_result=%s WHERE id=%s',(store.dumps(state),call['batch_id']))
            cur.execute('SELECT behavior_parse_result FROM knowledge_import_batch WHERE id=%s',(call['batch_id'],))
            if store.decode(cur.fetchone()['behavior_parse_result'])!=state:raise ValueError('迁移后校验失败')
        # Preserve old call IDs inside sources and backup; new references use batch_id only.
        cur.execute('UPDATE behavior_knowledge_point_detail SET source_call_id=NULL')
        conn.commit()
        cur.execute('DROP TABLE behavior_model_call');conn.commit()
        return {'status':'completed','calls':len(calls),'backup':str(path)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--summarize',action='store_true');args=p.parse_args()
    print(json.dumps(migrate(args.apply),ensure_ascii=False),flush=True)
    if args.summarize:
        if not args.apply:p.error('--summarize requires --apply')
        from services.behavior_knowledge_summary_service import refresh
        print(json.dumps(refresh(),ensure_ascii=False),flush=True)
