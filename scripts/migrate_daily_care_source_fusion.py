#!/usr/bin/env python3
"""Preview or migrate daily-care source rows and rebuild literal multi-model fusion.

Run with collectors stopped. Original table is retained under a timestamped name;
JSON backup includes schemas, source rows and original model batches. No model calls.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store
from services import daily_care_knowledge_service as care, daily_care_fusion_service as fusion


def migrate(apply=False,backup_root=None):
    with store.connect() as conn:
        lock=None
        try:
            with conn.cursor() as cur:
                lock=fusion.acquire_lock(cur)
                cur.execute('SHOW COLUMNS FROM daily_care_knowledge_point_detail');columns={r['Field'] for r in cur.fetchall()}
                legacy='point_data' in columns
                cur.execute('SELECT * FROM daily_care_knowledge_point_detail ORDER BY id');old=list(cur.fetchall())
                cur.execute("SELECT * FROM knowledge_import_batch WHERE knowledge_base='daily_care' ORDER BY id");batches=list(cur.fetchall())
                batch_map={b['id']:b for b in batches}
                if any(b['status']=='running' for b in batches):raise ValueError('仍有养护采集任务运行，请完成后迁移')
                dictionary=care.load_dictionary() if legacy else None
                rows=[]
                for old_row in old:
                    if not legacy:row=care.decode_row(dict(old_row))
                    else:
                        point=store.decode(old_row['point_data'])
                        row=care.source_row(point,dictionary)
                        for k in ('id','species_code','breed_code','life_stage_code','batch_id','point_key','created_at','updated_at','review_status'):
                            row[k]=old_row[k]
                    if row['batch_id'] not in batch_map:raise ValueError('发现缺少原始调用批次的来源记录')
                    rows.append(row)
                candidates=[]
                for row in rows:
                    b=batch_map[row['batch_id']]
                    if b['status']=='success':candidates.append({**row,'task_id':b['task_id'],'source_question':b['question'],'source_provider':b['provider'],'source_model':b['model']})
                preview=fusion.build_groups(candidates)
                result={'mode':'apply' if apply else 'preview','source_rows':len(rows),'fusion_rows':len(preview),
                        'multi_model_groups':sum(p['model_count']>1 for p in preview),'model_calls':0,'legacy_schema':legacy}
                if not apply:return result
                cur.execute('SHOW CREATE TABLE daily_care_knowledge_point_detail');schema=cur.fetchone()
                root=Path(backup_root or Path(__file__).resolve().parents[1]/'var/daily-care-backups');root.mkdir(parents=True,exist_ok=True)
                stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                backup=root/('source_fusion_'+stamp+'.json')
                with backup.open('x',encoding='utf-8') as out:json.dump({'schema':schema,'rows':old,'batches':batches},out,ensure_ascii=False,default=str)
                result['backup']=str(backup)
                if legacy:
                    shadow='daily_care_detail_v2_'+stamp
                    archive='daily_care_detail_backup_'+stamp
                    cur.execute(care.DDL.replace('IF NOT EXISTS daily_care_knowledge_point_detail','`'+shadow+'`'))
                    for row in rows:
                        keys=list(row)
                        cur.execute('INSERT INTO `'+shadow+'` ('+','.join('`'+k+'`' for k in keys)+') VALUES ('+','.join(['%s']*len(keys))+')',
                            [store.dumps(row[k]) if k in care.JSON_FIELDS else row[k] for k in keys])
                    cur.execute('SELECT * FROM `'+shadow+'` ORDER BY id');written=[care.decode_row(r) for r in cur.fetchall()]
                    if written!=rows:raise ValueError('迁移后逐字段校验失败，原表未替换')
                    conn.commit()
                    cur.execute('RENAME TABLE daily_care_knowledge_point_detail TO `'+archive+'`, `'+shadow+'` TO daily_care_knowledge_point_detail')
                    result['archive_table']=archive
                care.ensure_schema(cur)
                result['fusion']=fusion.rebuild(cur)
                conn.commit()
                return result
        except Exception:
            conn.rollback();raise
        finally:
            if lock:
                with conn.cursor() as cur:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--backup-root',type=Path)
    args=parser.parse_args()
    print(json.dumps(migrate(args.apply,args.backup_root),ensure_ascii=False,indent=2,default=str))


if __name__=='__main__':main()
