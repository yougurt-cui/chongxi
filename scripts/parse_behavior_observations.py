#!/usr/bin/env python3
"""Parse saved behavior model calls as an independent stage; no model API calls.

Preview: python scripts/parse_behavior_observations.py
Parse:   python scripts/parse_behavior_observations.py --apply --task-id 539
Archive historical calls only: add --archive-only to --apply.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store


def migrate(*, apply=False, backup_root=None, task_id=None, batch_id=None, archive_only=False):
    if task_id is not None and (not isinstance(task_id, int) or task_id < 1):
        raise ValueError('task_id必须为正整数')
    if batch_id is not None and (not isinstance(batch_id, int) or batch_id < 1):
        raise ValueError('batch_id必须为正整数')
    clauses = ["knowledge_base='behavior'", "status IN ('success','parse_failed','api_failed')"]
    params = []
    for column, value in (('task_id', task_id), ('id', batch_id)):
        if value is not None:
            clauses.append(column + '=%s'); params.append(value)
    # Preview reads only. No schema initialization, API calls, or data writes.
    with store.connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM knowledge_import_batch WHERE ' + ' AND '.join(clauses) + ' ORDER BY id', params)
        batches = list(cur.fetchall())
        details = []
        if batches:
            ids = [b['id'] for b in batches]
            cur.execute('SELECT * FROM behavior_knowledge_point_detail WHERE batch_id IN (' + ','.join(['%s'] * len(ids)) + ') ORDER BY id', ids)
            details = list(cur.fetchall())
        schemas = {}
        if apply:
            for table in ('knowledge_import_batch', 'behavior_knowledge_point_detail'):
                cur.execute('SHOW CREATE TABLE `' + table + '`'); schemas[table] = cur.fetchone()
    summary = {'mode': 'apply' if apply else 'preview', 'database': store.database_name(),
        'call_table': 'behavior_model_call', 'knowledge_table': 'behavior_knowledge_point_detail',
        'parse_log_table': 'behavior_knowledge_parse_run', 'batch_count': len(batches),
        'existing_points': len(details), 'model_calls': 0,
        'batches': [{'batch_id': b['id'], 'task_id': b['task_id'], 'provider': b['provider'],
                     'collection_status': 'success' if b['status'] == 'parse_failed' else b['status']} for b in batches]}
    if not apply:
        return summary
    root = Path(backup_root or Path(__file__).resolve().parents[1] / 'var/behavior-pipeline-backups')
    root.mkdir(parents=True, exist_ok=True)
    backup = root / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
    with backup.open('x', encoding='utf-8') as output:
        json.dump({'database': store.database_name(), 'schemas': schemas, 'batches': batches,
                   'knowledge_points': details}, output, ensure_ascii=False, default=str)
    store.ensure_database()
    from services.behavior_model_pipeline import ensure_call, parse_batch
    results = []; errors = []
    for batch in batches:
        try:
            call_id = ensure_call(batch['id'])
            if archive_only or batch['status'] == 'api_failed':
                results.append({'batch_id': batch['id'], 'source_call_id': call_id, 'status': 'archived'})
            else:
                results.append(parse_batch(batch['id']))
        except Exception as exc:
            errors.append({'batch_id': batch['id'], 'error': str(exc)})
    summary.update(backup=str(backup), results=results, errors=errors,
                   status='partial_failed' if errors else 'completed')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='执行归档和解析，缺省只预览')
    parser.add_argument('--task-id', type=int)
    parser.add_argument('--batch-id', type=int)
    parser.add_argument('--archive-only', action='store_true', help='仅归档原始调用，不解析知识')
    parser.add_argument('--backup-root', type=Path)
    args = parser.parse_args()
    result = migrate(apply=args.apply, backup_root=args.backup_root, task_id=args.task_id,
                     batch_id=args.batch_id, archive_only=args.archive_only)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get('errors'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
