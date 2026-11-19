#!/usr/bin/env python3
"""Preview or back up and atomically rename the selection summary table.

Stop API/collection workers before --apply. No model calls or content rebuilds.
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
OLD='knowledge_point_source'
NEW='knowledge_point'


def table_names(cur):
    cur.execute('SELECT TABLE_NAME name FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (%s,%s)',(OLD,NEW))
    return {r['name'] for r in cur.fetchall()}


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def snapshot(cur,table):
    cur.execute('SELECT * FROM `'+table+'` ORDER BY id');rows=list(cur.fetchall())
    cur.execute('SHOW CREATE TABLE `'+table+'`');ddl=cur.fetchone()['Create Table']
    cur.execute('SELECT AUTO_INCREMENT value FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s',(table,))
    return {'rows':rows,'ddl':ddl,'auto_increment':cur.fetchone()['value'],'sha256':fingerprint(rows)}


def migrate(*,apply=False,backup_root=None):
    with store.connect() as conn,conn.cursor() as cur:
        names=table_names(cur)
        if names=={NEW}:return {'status':'already_renamed','table':NEW,'model_calls':0}
        if names!={OLD}:raise ValueError('旧表缺失或新旧表同时存在，拒绝覆盖已有数据')
        lock='knowledge-assembly:'+store.database_name()
        acquired=False
        try:
            if apply:
                cur.execute('SELECT GET_LOCK(%s,0) acquired',(lock,));acquired=bool(cur.fetchone()['acquired'])
                if not acquired:raise ValueError('知识汇总正在运行，请稍后重试')
                if table_names(cur)!={OLD}:raise ValueError('表状态发生变化，请重新检查')
            cur.execute("SELECT COUNT(*) n FROM knowledge_import_batch WHERE knowledge_base='selection' AND status='running'")
            if cur.fetchone()['n']:raise ValueError('选粮采集仍在运行，请结束后重试')
            # MySQL does not rewrite SQL text inside views/triggers/routines/events.
            pattern=r'(^|[^a-zA-Z0-9_])knowledge_point_source([^a-zA-Z0-9_]|$)'
            for statement in (
                'SELECT TABLE_SCHEMA schema_name,TABLE_NAME object_name FROM information_schema.VIEWS WHERE VIEW_DEFINITION REGEXP %s',
                'SELECT TRIGGER_SCHEMA schema_name,TRIGGER_NAME object_name FROM information_schema.TRIGGERS WHERE ACTION_STATEMENT REGEXP %s',
                'SELECT ROUTINE_SCHEMA schema_name,ROUTINE_NAME object_name FROM information_schema.ROUTINES WHERE ROUTINE_DEFINITION REGEXP %s',
                'SELECT EVENT_SCHEMA schema_name,EVENT_NAME object_name FROM information_schema.EVENTS WHERE EVENT_DEFINITION REGEXP %s'):
                cur.execute(statement,(pattern,))
                dependencies=list(cur.fetchall())
                if dependencies:raise ValueError('检测到需同步修改的数据库对象：'+json.dumps(dependencies,ensure_ascii=False))
            before=snapshot(cur,OLD)
            result={'status':'preview','database':store.database_name(),'old_table':OLD,'new_table':NEW,'rows':len(before['rows']),'sha256':before['sha256'],'model_calls':0}
            if not apply:return result
            root=Path(backup_root or Path(__file__).resolve().parents[1]/'var/knowledge-point-rename-backups')
            root.mkdir(parents=True,exist_ok=True,mode=0o700)
            backup=root/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.json')
            with backup.open('x') as out:
                os.chmod(backup,0o600)
                json.dump({'database':store.database_name(),'old_table':OLD,'new_table':NEW,**before},out,ensure_ascii=False,default=str)
                out.flush();os.fsync(out.fileno())
            conn.commit()
            cur.execute('RENAME TABLE `knowledge_point_source` TO `knowledge_point`')
            after=snapshot(cur,NEW)
            if (before['sha256'],before['auto_increment'],before['ddl'].replace('`'+OLD+'`','`'+NEW+'`',1))!=(after['sha256'],after['auto_increment'],after['ddl']):
                raise RuntimeError('表已重命名但完整性校验未通过，请检查备份：'+str(backup))
            if table_names(cur)!={NEW}:raise RuntimeError('重命名后的表名校验失败')
            conn.commit()
            return {**result,'status':'completed','backup':str(backup),'verified':True}
        finally:
            if acquired:cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    print(json.dumps(migrate(apply=args.apply),ensure_ascii=False),flush=True)
