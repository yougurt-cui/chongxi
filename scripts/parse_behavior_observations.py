#!/usr/bin/env python3
"""Expose observation JSON as queryable columns without rewriting source/review data.

Preview: python scripts/parse_behavior_observations.py
Apply:   python scripts/parse_behavior_observations.py --apply
Requires MySQL 8+. New records are projected automatically by stored generated columns.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store

TABLE = 'behavior_knowledge_point_detail'
TEXT_PATHS = {
    'breed': ('$.breed', 120),
    'life_stage': ('$.life_stage', 120),
    'observation_code': ('$.observation.observation_code', 160),
    'observation_name': ('$.observation.observation_name', 160),
}
ARRAY_PATHS = {name: '$.observation.' + name for name in
               ('normal_scenarios', 'abnormal_scenarios', 'unmapped_health_issues')}


def definitions():
    result = {}
    for name, (path, maximum) in TEXT_PATHS.items():
        # JSON null and a missing path become SQL NULL, never the string "null".
        expression = f"CASE WHEN JSON_TYPE(JSON_EXTRACT(point_data,'{path}'))='STRING' THEN JSON_UNQUOTE(JSON_EXTRACT(point_data,'{path}')) ELSE NULL END"
        result[name] = f'VARCHAR({maximum}) GENERATED ALWAYS AS ({expression}) STORED'
    for name, path in ARRAY_PATHS.items():
        expression = f"CASE WHEN JSON_TYPE(JSON_EXTRACT(point_data,'{path}'))='ARRAY' THEN JSON_EXTRACT(point_data,'{path}') ELSE NULL END"
        result[name] = f'JSON GENERATED ALWAYS AS ({expression}) STORED'
    return result


def project(point):
    if not isinstance(point, dict):
        raise ValueError('point_data必须为JSON对象')
    observation = point.get('observation')
    if observation is None:
        observation = {}
    if not isinstance(observation, dict):
        raise ValueError('observation必须为对象')
    result = {'species': point.get('species')}
    for name, (_, maximum) in TEXT_PATHS.items():
        value = point.get(name) if name in {'breed', 'life_stage'} else observation.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError(name + '必须为字符串且长度不得超过' + str(maximum))
        result[name] = value
    for name in ARRAY_PATHS:
        value = observation.get(name)
        if value is not None and not isinstance(value, list):
            raise ValueError(name + '必须为数组')
        result[name] = value
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def migrate(*, apply=False, backup_root=None):
    # Do not call ensure_database: a preview must not create or alter any tables.
    with store.connect() as conn, conn.cursor() as cur:
        locked = False
        try:
            if apply:
                cur.execute("SELECT GET_LOCK('behavior-observation-columns',10) acquired")
                locked = bool(cur.fetchone()['acquired'])
                if not locked:
                    raise RuntimeError('另一个行为字段展开脚本正在执行')
            cur.execute(f'SHOW FULL COLUMNS FROM `{TABLE}`')
            columns = {r['Field']: r for r in cur.fetchall()}
            required = {'id', 'batch_id', 'species', 'point_data', 'review_status'}
            if not required.issubset(columns):
                raise RuntimeError('源表结构不符合预期')
            cur.execute(f'SHOW CREATE TABLE `{TABLE}`')
            schema = cur.fetchone()
            cur.execute(f'SELECT * FROM `{TABLE}` ORDER BY id')
            rows = list(cur.fetchall())
            expected = {}; samples = []; observation_count = 0
            for row in rows:
                try:
                    point = store.decode(row['point_data'])
                    parsed = project(point)
                    if parsed['species'] != row['species']:
                        raise ValueError('species列与point_data不一致，停止以避免掩盖来源差异')
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"来源记录 #{row['id']}: {exc}") from exc
                expected[row['id']] = parsed
                observation_count += int(isinstance(point.get('observation'), dict))
                if len(samples) < 10:
                    samples.append({'id': row['id'], 'batch_id': row['batch_id'],
                        **{k: parsed[k] for k in ('species', *TEXT_PATHS)},
                        **{name + '_count': len(parsed[name]) if parsed[name] is not None else None for name in ARRAY_PATHS}})
            # Reject schema drift rather than overwriting an ordinary/manual column.
            for name in definitions():
                if name in columns:
                    if 'STORED GENERATED' not in columns[name]['Extra'].upper():
                        raise RuntimeError(f'{name}已存在但不是存储生成列，需人工检查')
                    cur.execute('''SELECT GENERATION_EXPRESSION AS generated_expression FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s''', (TABLE, name))
                    expression = cur.fetchone()['generated_expression']
                    path = TEXT_PATHS[name][0] if name in TEXT_PATHS else ARRAY_PATHS[name]
                    if path not in expression or 'point_data' not in expression:
                        raise RuntimeError(name + '已存在但来源表达式不一致')
            missing = [name for name in definitions() if name not in columns]
            clauses = [f'ADD COLUMN `{name}` {definitions()[name]}' for name in missing]
            cur.execute(f'SHOW INDEX FROM `{TABLE}`')
            if not any(r['Key_name'] == 'idx_behavior_observation_grain' for r in cur.fetchall()):
                clauses.append('ADD INDEX idx_behavior_observation_grain (species,breed,life_stage,observation_code,observation_name)')
            sql = f'ALTER TABLE `{TABLE}`\n  ' + ',\n  '.join(clauses) if clauses else None
            result = {'database': store.database_name(), 'table': TABLE, 'mode': 'apply' if apply else 'preview',
                      'rows': len(rows), 'observation_rows': observation_count, 'new_columns': missing,
                      'sql': sql, 'samples': samples}
            if not apply:
                return result
            # Back up schema and rows before DDL (MySQL ALTER TABLE commits implicitly).
            root = Path(backup_root or Path(__file__).resolve().parents[1] / 'var/behavior-observation-backups')
            root.mkdir(parents=True, exist_ok=True)
            backup = root / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
            with backup.open('x', encoding='utf-8') as output:
                json.dump({'database': store.database_name(), 'schema': schema, 'rows': rows}, output,
                          ensure_ascii=False, default=str)
            conn.commit()  # End the read snapshot before DDL.
            if sql:
                cur.execute(sql)
            conn.commit()
            cur.execute(f'SELECT * FROM `{TABLE}` ORDER BY id')
            after = {r['id']: r for r in cur.fetchall()}
            for old in rows:
                row = after.get(old['id'])
                if row is None:
                    raise RuntimeError(f"来源记录 #{old['id']}已被并发删除，请检查备份")
                # Historical content, identifiers and manual review must be untouched.
                if any(canonical(row[k]) != canonical(v) for k, v in old.items()):
                    raise RuntimeError(f"来源记录 #{old['id']}已被并发更新，请检查备份")
                for name in (*TEXT_PATHS, *ARRAY_PATHS):
                    actual = store.decode(row[name]) if name in ARRAY_PATHS else row[name]
                    if canonical(actual) != canonical(expected[old['id']][name]):
                        raise RuntimeError(f"记录 #{old['id']}字段 {name} 与原JSON不一致")
            result.update(backup=str(backup), verified_rows=len(rows),
                          source_unchanged=True, generated_columns_automatic=True)
            return result
        finally:
            if locked:
                cur.execute("SELECT RELEASE_LOCK('behavior-observation-columns')")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='备份后执行字段展开；不传则只预览')
    parser.add_argument('--backup-root', type=Path, help='备份目录，默认var/behavior-observation-backups')
    args = parser.parse_args()
    print(json.dumps(migrate(apply=args.apply, backup_root=args.backup_root), ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
