"""Back up, preserve candidate IDs, and atomically replace the solution main table.

Run with collection/API workers stopped. Default is a read-only preview.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store
from services import solution_scene_service as scenes

MAIN = 'solution_knowledge_point'
DETAIL = 'solution_knowledge_point_detail'
STAGING = 'solution_knowledge_point_scene_staging'
ARCHIVE = 'solution_knowledge_point_legacy_scene_v1'


def exists(cur, table):
    cur.execute('SELECT COUNT(*) n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s', (table,))
    return bool(cur.fetchone()['n'])


def snapshot(cur, table):
    cur.execute('SHOW CREATE TABLE '+table)
    schema = cur.fetchone()['Create Table']
    cur.execute('SELECT * FROM '+table+' ORDER BY id')
    return {'schema': schema, 'rows': list(cur.fetchall())}


def backup(cur, directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    data = {}
    for table in (MAIN, DETAIL, 'health_solution'):
        if exists(cur, table):
            data[table] = snapshot(cur, table)
    cur.execute("SELECT * FROM knowledge_import_batch WHERE knowledge_base='solution' ORDER BY id")
    data['solution_batches'] = list(cur.fetchall())
    path = directory / 'before.json'
    with path.open('x', encoding='utf-8') as f:
        os.chmod(path, 0o600)
        json.dump(data, f, ensure_ascii=False, default=str, indent=2)
        f.flush(); os.fsync(f.fileno())
    # Exact SQL for restoring the existing tables, independent of Python types.
    sql = directory / 'restore.sql'
    with sql.open('x', encoding='utf-8') as f:
        os.chmod(sql, 0o600)
        f.write('-- Stop API/collection workers before restoring. Batch rows were not changed.\nSET FOREIGN_KEY_CHECKS=0;\n')
        for table, saved in data.items():
            if table == 'solution_batches':
                continue
            f.write('DROP TABLE IF EXISTS `'+table+'`;\n'+saved['schema']+';\n')
            for row in saved['rows']:
                statement = 'INSERT INTO `'+table+'` ('+','.join('`'+k+'`' for k in row)+') VALUES ('+','.join(['%s']*len(row))+')'
                f.write(cur.mogrify(statement, tuple(row.values()))+';\n')
        f.write('SET FOREIGN_KEY_CHECKS=1;\n')
        f.flush(); os.fsync(f.fileno())
    return str(path)


def migrate(conn, *, apply=False, backup_dir=None):
    with conn.cursor() as cur:
        cur.execute('SHOW COLUMNS FROM '+MAIN)
        legacy = 'batch_id' in {r['Field'] for r in cur.fetchall()}
        source = MAIN if legacy else DETAIL
        cur.execute('''SELECT p.*,b.task_id,b.provider,b.model,b.question FROM '''+source+''' p
            JOIN knowledge_import_batch b ON b.id=p.batch_id
            WHERE b.knowledge_base='solution' AND b.status='success' ORDER BY p.id''')
        rows = list(cur.fetchall())
        groups = scenes.source_groups(rows)
        result = {'legacy': legacy, 'source_rows': len(rows), 'scene_rows': len(groups),
                  'classified_rows': sum(len(v) for v in groups.values()),
                  'unclassified_rows': len(rows)-sum(len(v) for v in groups.values())}
        if not apply:
            result['scenes'] = [dict(zip(scenes.GRAIN, key)) for key in groups]
            return result
        if not backup_dir:
            raise ValueError('--apply requires --backup-dir')
        cur.execute("SELECT COUNT(*) n FROM knowledge_import_batch WHERE knowledge_base='solution' AND status='running'")
        if cur.fetchone()['n']:
            raise ValueError('解决方案采集正在运行，不能迁移')
        result['backup'] = backup(cur, Path(backup_dir))
        if not legacy:
            result.update(scenes.refresh(conn));conn.commit()
            return result
        if exists(cur, ARCHIVE) or exists(cur, STAGING):
            raise ValueError('已有迁移归档或暂存表，请核对上次执行状态')
        if exists(cur, DETAIL):
            # May exist empty after an old service schema check. Never overwrite data.
            cur.execute('SELECT COUNT(*) n FROM '+DETAIL)
            if cur.fetchone()['n']:
                raise ValueError('明细表非空，需先核对，不能覆盖')
        else:
            cur.execute('CREATE TABLE '+DETAIL+' LIKE '+MAIN)
        cur.execute('INSERT INTO '+DETAIL+' SELECT * FROM '+MAIN)
        conn.commit()
        original = snapshot(cur, MAIN)['rows']
        copied = snapshot(cur, DETAIL)['rows']
        if copied != original:
            raise ValueError('候选明细逐字段校验失败，尚未切换主表')
        cur.execute(scenes.DDL.replace('CREATE TABLE IF NOT EXISTS '+MAIN, 'CREATE TABLE '+STAGING, 1))
        scenes.refresh(conn, table=STAGING)
        conn.commit()
        cur.execute('SELECT * FROM '+STAGING+' ORDER BY id')
        built = list(cur.fetchall())
        if len(built) != len(groups):
            raise ValueError('场景行数校验失败，尚未切换主表')
        seen = set()
        for row in built:
            key = tuple(row[k] for k in scenes.GRAIN)
            expected = scenes.materialize(key, groups[key])
            if row['input_hash'] != expected['input_hash']:
                raise ValueError('场景来源校验失败，尚未切换主表')
            seen.update(s['candidate_id'] for s in store.decode(row['sources']))
        if seen != {r['id'] for r in rows if r.get('scenario_data')}:
            raise ValueError('场景来源遗漏，尚未切换主表')
        cur.execute('RENAME TABLE '+MAIN+' TO '+ARCHIVE+', '+STAGING+' TO '+MAIN)
        result['detail_rows'] = len(copied)
        result['archive_table'] = ARCHIVE
        result['backup_sha256'] = hashlib.sha256(Path(result['backup']).read_bytes()).hexdigest()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    with store.connect() as conn:
        name = 'solution-import:'+hashlib.sha256(store.database_name().encode()).hexdigest()[:40]
        with conn.cursor() as cur:
            if args.apply:
                cur.execute('SELECT GET_LOCK(%s,10) acquired', (name,))
                if not cur.fetchone()['acquired']:
                    raise ValueError('解决方案导入繁忙，未进行迁移')
        try:
            result = migrate(conn, apply=args.apply, backup_dir=args.backup_dir)
            print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        finally:
            if args.apply:
                with conn.cursor() as cur:
                    cur.execute('SELECT RELEASE_LOCK(%s)', (name,))


if __name__ == '__main__':
    main()
