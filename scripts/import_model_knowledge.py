#!/usr/bin/env python3
"""Backfill existing model responses; never calls an LLM or writes legacy KB tables."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as service


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--task-id', type=int)
    group.add_argument('--all', action='store_true', help='导入所有已有成功模型响应，重复运行不重复入库')
    args = parser.parse_args()
    service.ensure_database()
    if args.task_id is not None:
        task_ids = [args.task_id]
    else:
        with service.collector.mysql_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT task_id FROM knowledge_import_batch WHERE status='success' ORDER BY task_id")
                task_ids = [r['task_id'] for r in cur.fetchall()]
    print('Independent knowledge database:', service.ensure_database())
    failed = False
    for task_id in task_ids:
        result = service.import_task(task_id)
        print(json.dumps(result, ensure_ascii=False, default=str))
        failed = failed or bool(result['errors'])
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
