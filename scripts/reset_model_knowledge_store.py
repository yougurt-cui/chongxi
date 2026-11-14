#!/usr/bin/env python3
"""Explicit reset to the two-table layout; old data is backed up, never migrated."""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import pymysql

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app_config import get_mysql_config
from services import model_knowledge_import_service as store

KB_TABLES={'knowledge_import_batch','knowledge_point_source','knowledge_point_candidate','knowledge_response_search'}
OLD_COLLECTION={'distill_task','distill_model_response','distill_knowledge_candidate','distill_task_options','distill_model_search'}


def tables(cur):
    cur.execute('SHOW TABLES');return {next(iter(row.values())) for row in cur.fetchall()}


def reset(backup_dir,remove_old_collection=True):
    target=store.database_name()
    backup_dir=Path(backup_dir);backup_dir.mkdir(parents=True,exist_ok=False,mode=0o700)
    source_config=get_mysql_config(database=os.getenv('MULTI_MODEL_MYSQL_DATABASE') or None)
    source=pymysql.connect(**source_config,cursorclass=pymysql.cursors.DictCursor,autocommit=False)
    destination=store.connect()
    try:
        with destination.cursor() as cur:
            current=tables(cur)
            if current-KB_TABLES:raise RuntimeError('独立库存在其他表，停止重建：'+str(current-KB_TABLES))
            if 'knowledge_import_batch' in current:
                cur.execute("SELECT COUNT(*) AS n FROM knowledge_import_batch WHERE status='running'")
                if cur.fetchone()['n']:raise RuntimeError('仍有采集或导入任务运行，请完成后再重建')
        with source.cursor() as cur:
            legacy=tables(cur)&OLD_COLLECTION
            if 'distill_task' in legacy:
                cur.execute("SELECT COUNT(*) AS n FROM distill_task WHERE status='running'")
                if cur.fetchone()['n']:raise RuntimeError('仍有旧采集任务运行，请完成后再重建')
        backups={}
        for label,conn,names in [('knowledge',destination,current),('legacy_collection',source,legacy if remove_old_collection else set())]:
            with conn.cursor() as cur:
                # Refuse to break an unrelated table's foreign keys.
                if names:
                    placeholders=','.join(['%s']*len(names))
                    cur.execute('''SELECT TABLE_NAME,REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE
                        WHERE REFERENCED_TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IN ('''+placeholders+')',tuple(names))
                    for row in cur.fetchall():
                        if row['TABLE_NAME'] not in names:raise RuntimeError('其他表引用采集表，停止重建：'+row['TABLE_NAME'])
                snapshots={}
                for table in sorted(names):
                    cur.execute('SHOW CREATE TABLE `'+table+'`');ddl=cur.fetchone()['Create Table']
                    cur.execute('SELECT * FROM `'+table+'`')
                    snapshots[table]={'ddl':ddl,'rows':list(cur.fetchall())}
                encoded=json.dumps(snapshots,ensure_ascii=False,default=str).encode()
                path=backup_dir/(label+'.json');path.write_bytes(encoded);path.chmod(0o600)
                backups[label]={'tables':{k:len(v['rows']) for k,v in snapshots.items()},'sha256':hashlib.sha256(encoded).hexdigest()}
        (backup_dir/'manifest.json').write_text(json.dumps({'target':target,'backups':backups},ensure_ascii=False,indent=2))
        with destination.cursor() as cur:
            for table in ['knowledge_point_source','knowledge_response_search','knowledge_point_candidate','knowledge_import_batch']:
                cur.execute('DROP TABLE IF EXISTS `'+table+'`')
        destination.commit()
        if remove_old_collection:
            with source.cursor() as cur:
                for table in ['distill_knowledge_candidate','distill_model_search','distill_task_options','distill_model_response','distill_task']:
                    cur.execute('DROP TABLE IF EXISTS `'+table+'`')
            source.commit()
        store._ensure_database.cache_clear();store.ensure_database()
        with destination.cursor() as cur:
            actual=tables(cur)
            assert actual=={'knowledge_import_batch','knowledge_point_source'},actual
            for table in actual:
                cur.execute('SELECT COUNT(*) AS n FROM `'+table+'`');assert cur.fetchone()['n']==0
        return {'database':target,'tables':sorted(actual),'status':'reset_empty','backup':str(backup_dir)}
    finally:destination.close();source.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--reset',action='store_true',required=True)
    parser.add_argument('--backup-dir',required=True)
    args=parser.parse_args();print(json.dumps(reset(args.backup_dir),ensure_ascii=False))
