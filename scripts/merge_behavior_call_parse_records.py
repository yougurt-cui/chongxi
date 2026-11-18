#!/usr/bin/env python3
"""Merge parsing metadata/history into behavior_model_call, backing up then removing the old table."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store
from services.behavior_model_pipeline import PARSE_FIELDS, ensure_schema, parse_lock_name

OLD_TABLE = 'behavior_knowledge_parse_run'


def legacy_exists(cur):
    cur.execute('''SELECT COUNT(*) n FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s''', (OLD_TABLE,))
    return bool(cur.fetchone()['n'])


def merge_record(call, runs):
    history = store.decode(call.get('parse_history')) or []
    if not isinstance(history, list):
        raise ValueError('parse_history必须为数组')
    known = {e.get('legacy_parse_run_id') for e in history if isinstance(e, dict)}
    additions = [r for r in runs if r['id'] not in known]
    if any(r['status'] == 'running' for r in runs) or call.get('parse_status') == 'running':
        raise ValueError('仍有解析任务运行或中断，请确认任务状态后再迁移')
    result = {name: call.get(name) for name in PARSE_FIELDS}
    result['parse_status'] = call.get('parse_status') or 'pending'
    result['parse_attempts'] = (call.get('parse_attempts') or 0) + sum(r['attempts'] for r in additions)
    result['point_count'] = call.get('point_count') or 0
    result['parse_history'] = history + [dict(json.loads(json.dumps(r, default=str)),
        legacy_parse_run_id=r['id'], legacy_table=OLD_TABLE) for r in additions]
    matching = [r for r in runs if r['source_hash'] == call['response_hash']]
    if matching:
        latest = max(matching, key=lambda r: (r['finished_at'] or r['started_at'], r['id']))
        if not call.get('parsed_at') or latest['finished_at'] and latest['finished_at'] > call['parsed_at']:
            result.update(parse_status=latest['status'], parse_source_hash=latest['source_hash'],
                parser_version=latest['parser_version'], point_count=latest['point_count'],
                parse_error=latest['error_message'], parse_started_at=latest['started_at'],
                parsed_at=latest['finished_at'])
    return result


def migrate(*, apply=False, backup_root=None):
    with store.connect() as conn, conn.cursor() as cur:
        locks = []
        try:
            if apply:
                cur.execute("SELECT GET_LOCK('merge-behavior-call-parse',10) acquired")
                if not cur.fetchone()['acquired']:
                    raise RuntimeError('另一个合并脚本正在执行')
                locks.append('merge-behavior-call-parse')
            if not legacy_exists(cur):
                return {'status':'already_merged','table':'behavior_model_call','model_calls':0}
            cur.execute('SELECT * FROM behavior_knowledge_parse_run ORDER BY id')
            runs = list(cur.fetchall())
            cur.execute('SELECT * FROM behavior_model_call ORDER BY id')
            calls = {r['id']: r for r in cur.fetchall()}
            grouped = {}
            for run in runs:
                if run['source_call_id'] not in calls:
                    raise ValueError('解析记录没有对应调用记录：' + str(run['id']))
                grouped.setdefault(run['source_call_id'], []).append(run)
            locked_ids = set(grouped)
            for call_id in sorted(grouped):
                if apply:
                    lock = parse_lock_name(call_id)
                    cur.execute('SELECT GET_LOCK(%s,0) acquired', (lock,))
                    if not cur.fetchone()['acquired']:
                        raise RuntimeError('调用记录正在解析：' + str(call_id))
                    locks.append(lock)
            # Refresh after locking; a parser may have finished during discovery.
            if apply:
                cur.execute('SELECT * FROM behavior_knowledge_parse_run ORDER BY id')
                runs = list(cur.fetchall()); grouped = {}
                for run in runs:grouped.setdefault(run['source_call_id'], []).append(run)
                if any(i not in locked_ids for i in grouped):
                    raise RuntimeError('发现新的调用记录，请重新运行迁移')
                cur.execute('SELECT * FROM behavior_model_call ORDER BY id')
                calls = {r['id']: r for r in cur.fetchall()}
            planned = {i: merge_record(calls[i], rows) for i, rows in grouped.items()}
            summary = {'mode':'apply' if apply else 'preview','call_rows':len(calls),
                'parse_rows':len(runs),'merged_calls':len(planned),'target_table':'behavior_model_call',
                'old_table':OLD_TABLE,'model_calls':0}
            if not apply:return summary
            schemas = {}
            for table in ('behavior_model_call', OLD_TABLE):
                cur.execute('SHOW CREATE TABLE `' + table + '`');schemas[table]=cur.fetchone()
            root = Path(backup_root or Path(__file__).resolve().parents[1] / 'var/behavior-call-merge-backups')
            root.mkdir(parents=True, exist_ok=True)
            backup = root / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
            with backup.open('x', encoding='utf-8') as output:
                json.dump({'schemas':schemas,'calls':list(calls.values()),'parse_runs':runs},
                          output, ensure_ascii=False, default=str)
            conn.commit()
            ensure_schema(cur); conn.commit()
            names = list(PARSE_FIELDS)
            for call_id, values in planned.items():
                cur.execute('UPDATE behavior_model_call SET ' + ','.join('`'+n+'`=%s' for n in names) + ' WHERE id=%s',
                    tuple(store.dumps(values[n]) if n=='parse_history' else values[n] for n in names) + (call_id,))
            conn.commit()
            cur.execute('SELECT * FROM behavior_model_call ORDER BY id')
            after = {r['id']:r for r in cur.fetchall()}
            for call_id, old in calls.items():
                assert all(after[call_id][k]==v for k,v in old.items() if k not in PARSE_FIELDS), '原始调用字段发生变化'
            for call_id, rows in grouped.items():
                history = store.decode(after[call_id]['parse_history'])
                stored = {e.get('legacy_parse_run_id'):e for e in history if isinstance(e,dict)}
                for row in rows:
                    wanted=json.loads(json.dumps(row,default=str))
                    assert all(stored[row['id']][k]==v for k,v in wanted.items()), '解析历史校验失败'
            # Every legacy row is now preserved in parse_history and in the backup file.
            cur.execute('DROP TABLE behavior_knowledge_parse_run')
            conn.commit()
            return {**summary,'status':'completed','backup':str(backup),
                    'raw_fields_unchanged':True,'legacy_history_preserved':True,'old_table_removed':True}
        finally:
            for lock in reversed(locks):cur.execute('SELECT RELEASE_LOCK(%s)',(lock,))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--backup-root',type=Path)
    args=parser.parse_args()
    print(json.dumps(migrate(apply=args.apply,backup_root=args.backup_root),ensure_ascii=False,indent=2,default=str))
