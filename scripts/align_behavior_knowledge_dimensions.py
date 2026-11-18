#!/usr/bin/env python3
"""Align behavior knowledge field names/order with health knowledge, preserving raw source data."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store
from services.behavior_knowledge_schema import FIELDS, GENERATED_FIELDS, JSON_FIELDS, WRITE_FIELDS, create_ddl
from services.behavior_knowledge_dimensions import load_dictionary, values_for
from services.behavior_model_pipeline import parse_lock_name

TABLE='behavior_knowledge_point_detail'


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)


def aligned_row(old, batch, dictionary):
    point=store.decode(old['point_data'])
    dims=values_for(point,batch,dictionary,call_id=old.get('source_call_id'),
                    source_hash=old.get('source_hash'),parser_version=old.get('parser_version'))
    result={name:old.get(name) for name in FIELDS if name not in GENERATED_FIELDS}
    result.update(dims)
    result['point_data']=point
    result['updated_at']=old.get('updated_at') or datetime.now()
    return result


def migrate(*, apply=False, backup_root=None):
    dictionary=load_dictionary()
    locks=[]
    with store.connect() as conn,conn.cursor() as cur:
        try:
            if apply:
                cur.execute("SELECT GET_LOCK('align-behavior-knowledge-dimensions',10) acquired")
                if not cur.fetchone()['acquired']:raise RuntimeError('另一个维度迁移正在执行')
                locks.append('align-behavior-knowledge-dimensions')
                cur.execute('SELECT id FROM behavior_model_call ORDER BY id')
                for call in cur.fetchall():
                    lock=parse_lock_name(call['id'])
                    cur.execute('SELECT GET_LOCK(%s,0) acquired',(lock,))
                    if not cur.fetchone()['acquired']:raise RuntimeError('有行为知识正在解析，请稍后重试')
                    locks.append(lock)
            cur.execute('SHOW COLUMNS FROM '+TABLE);columns=[r['Field'] for r in cur.fetchall()]
            unknown=set(columns)-set(FIELDS)-{'species','breed','life_stage','category'}
            if unknown:raise ValueError('存在未识别字段，停止以免丢失：'+','.join(sorted(unknown)))
            cur.execute('SELECT * FROM '+TABLE+' ORDER BY id');before=list(cur.fetchall())
            cur.execute("SELECT * FROM knowledge_import_batch WHERE knowledge_base='behavior'")
            batches={r['id']:r for r in cur.fetchall()}
            wanted=[aligned_row(r,batches[r['batch_id']],dictionary) for r in before]
            needs_change=columns!=list(FIELDS)
            if not needs_change:
                for old,new in zip(before,wanted):
                    if any(canonical(store.decode(old[k]) if k in JSON_FIELDS else old[k])!=canonical(new[k])
                           for k in WRITE_FIELDS):needs_change=True;break
            summary={'mode':'apply' if apply else 'preview','table':TABLE,'rows':len(before),
                'field_order':list(FIELDS),'changed':needs_change,
                'samples':[{k:r[k] for k in ('id','species_code','breed_code','life_stage_code','category_code','category_name')} for r in wanted[:10]],
                'model_calls':0}
            if not apply or not needs_change:return summary
            cur.execute('''SELECT COUNT(*) n FROM information_schema.KEY_COLUMN_USAGE
                WHERE REFERENCED_TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME=%s''',(TABLE,))
            if cur.fetchone()['n']:raise RuntimeError('存在入向外键，需先安排外键迁移')
            cur.execute('SHOW CREATE TABLE '+TABLE);schema=cur.fetchone()
            cur.execute('SHOW TABLE STATUS LIKE %s',(TABLE,));next_id=cur.fetchone()['Auto_increment']
            root=Path(backup_root or Path(__file__).resolve().parents[1]/'var/behavior-dimension-backups')
            root.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            backup=root/(stamp+'.json')
            with backup.open('x',encoding='utf-8') as output:
                json.dump({'schema':schema,'rows':before,'dictionary':dictionary},output,ensure_ascii=False,default=str)
            work=TABLE+'_work_'+stamp;old_table=TABLE+'_old_'+stamp
            conn.commit()
            cur.execute(create_ddl(work))
            names=[n for n in FIELDS if n not in GENERATED_FIELDS]
            for row in wanted:
                cur.execute('INSERT INTO `'+work+'` ('+','.join('`'+n+'`' for n in names)+') VALUES ('+
                    ','.join(['%s']*len(names))+')',tuple(store.dumps(row[n]) if n in JSON_FIELDS else row[n] for n in names))
            conn.commit()
            if next_id:cur.execute('ALTER TABLE `'+work+'` AUTO_INCREMENT='+str(int(next_id)))
            cur.execute('SELECT * FROM `'+work+'` ORDER BY id');prepared=list(cur.fetchall())
            for old,new in zip(before,prepared):
                for key in ('id','batch_id','source_call_id','point_key','source_hash','parser_version','behavior_name','review_status','created_at'):
                    assert old.get(key)==new.get(key),(key,old['id'])
                assert store.decode(old['point_data'])==store.decode(new['point_data'])
                for key in GENERATED_FIELDS:
                    a=store.decode(old[key]) if key in ('normal_scenarios','abnormal_scenarios','unmapped_health_issues') else old[key]
                    b=store.decode(new[key]) if key in ('normal_scenarios','abnormal_scenarios','unmapped_health_issues') else new[key]
                    assert a==b,(key,old['id'])
            assert len(prepared)==len(before)
            # Refuse cutover if a writer changed data after the snapshot.
            cur.execute('SELECT * FROM '+TABLE+' ORDER BY id')
            assert canonical(list(cur.fetchall()))==canonical(before),'源表被并发修改，请停止解析后重试'
            conn.commit()
            cur.execute('RENAME TABLE `'+TABLE+'` TO `'+old_table+'`, `'+work+'` TO `'+TABLE+'`')
            cur.execute('SHOW COLUMNS FROM '+TABLE)
            assert [r['Field'] for r in cur.fetchall()]==list(FIELDS)
            cur.execute('SELECT COUNT(*) n FROM '+TABLE);assert cur.fetchone()['n']==len(before)
            cur.execute('DROP TABLE `'+old_table+'`');conn.commit()
            return {**summary,'backup':str(backup),'source_json_unchanged':True,
                    'knowledge_ids_unchanged':True,'review_status_unchanged':True}
        finally:
            for lock in reversed(locks):cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--backup-root',type=Path)
    args=parser.parse_args()
    print(json.dumps(migrate(apply=args.apply,backup_root=args.backup_root),ensure_ascii=False,indent=2,default=str))
