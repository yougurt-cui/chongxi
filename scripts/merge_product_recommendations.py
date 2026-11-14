#!/usr/bin/env python3
"""Migrate source-level product rows to six-dimension groups with full provenance."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import product_recommendation_import_service as service


def snapshot_hash(rows):
    return service.digest(json.loads(json.dumps(rows, ensure_ascii=False, default=str)))


def legacy_records(rows):
    records = []
    for row in rows:
        if row['review_status'] != 'pending' or row['verification_status'] != 'pending':
            raise ValueError('存在人工审核记录，不能自动迁移合并')
        record = row['raw_json']
        if isinstance(record, str): record = json.loads(record)
        if not isinstance(record, dict) or not record.get('source_key') or not record.get('recommendations'):
            raise ValueError('旧记录缺少可重建的标准化来源，已停止迁移')
        records.append(record)
    return records


def migrate(*, apply=False, backup_dir=None):
    with service.target_connect() as conn, conn.cursor() as cur:
        acquired, stage_created, swapped = False, False, False
        stage = 'product_recommendation_stage_' + uuid.uuid4().hex[:12]
        backup = 'product_recommendation_backup_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
        try:
            if apply:
                cur.execute('SELECT GET_LOCK(%s,10) acquired', (service.lock_name(),))
                acquired = bool(cur.fetchone()['acquired'])
                if not acquired: raise ValueError('产品推荐导入正在运行，请稍后重试')
            cur.execute('SHOW COLUMNS FROM product_recommendation')
            columns = {r['Field'] for r in cur.fetchall()}
            if 'recommendation_key' in columns:
                cur.execute('SELECT COUNT(*) n FROM product_recommendation')
                return {'status': 'already_migrated', 'groups': cur.fetchone()['n']}
            cur.execute('SELECT * FROM product_recommendation ORDER BY id')
            rows = list(cur.fetchall())
            records = legacy_records(rows)
            groups = service.group_records(records)
            summary = {'mode': 'apply' if apply else 'dry-run', 'old_rows': len(rows),
                       'recommendation_entries': sum(len(r['recommendations']) for r in records),
                       'groups': len(groups), 'source_records': len({r['source_key'] for r in records}),
                       'products': len({r['product_key'] for r in records}),
                       'source_links': sum(g['source_count'] for g in groups)}
            if not apply: return summary
            cur.execute('SELECT TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME=%s',
                        (service.target_database(), 'product_recommendation'))
            if cur.fetchall(): raise ValueError('存在引用旧行ID的外键，请先处理依赖后迁移')
            cur.execute('SHOW CREATE TABLE product_recommendation')
            ddl = cur.fetchone()['Create Table']
            directory = Path(backup_dir or Path(__file__).resolve().parents[1] / 'var/product-import-backups')
            directory.mkdir(parents=True, exist_ok=True)
            backup_file = directory / (backup + '.json')
            with backup_file.open('x', encoding='utf-8') as out:
                json.dump({'database': service.target_database(), 'ddl': ddl, 'rows': rows,
                           'snapshot_hash': snapshot_hash(rows)}, out, ensure_ascii=False, indent=2, default=str)
            conn.commit()
            sql = '\n'.join(line for line in service.SCHEMA.read_text().splitlines() if not line.lstrip().startswith('--'))
            statements = [s.strip() for s in sql.split(';') if s.strip().startswith('CREATE TABLE')]
            if len(statements) != 1: raise ValueError('迁移要求单表结构')
            cur.execute(statements[0].replace('CREATE TABLE IF NOT EXISTS product_recommendation', 'CREATE TABLE ' + stage, 1))
            stage_created = True
            for group in groups: service.write_record(conn, group, table=stage)
            conn.commit()
            cur.execute(f'SELECT COUNT(*) n FROM {stage}')
            if cur.fetchone()['n'] != len(groups): raise ValueError('暂存表数量不一致')
            cur.execute(f'SELECT raw_json FROM {stage}')
            recovered = service.stored_records(cur.fetchall())
            if recovered != {r['source_key']: r for r in records}:
                raise ValueError('来源追溯校验失败')
            # Fresh snapshot detects a concurrent writer using the legacy importer.
            conn.commit()
            cur.execute('SELECT * FROM product_recommendation ORDER BY id FOR UPDATE')
            if snapshot_hash(cur.fetchall()) != snapshot_hash(rows):
                raise ValueError('迁移期间旧表发生变化，请重新执行')
            cur.execute(f'RENAME TABLE product_recommendation TO {backup}, {stage} TO product_recommendation')
            swapped = True
            return {**summary, 'status': 'completed', 'backup_table': backup, 'backup_file': str(backup_file)}
        except Exception:
            conn.rollback()
            if stage_created and not swapped:
                cur.execute(f'DROP TABLE IF EXISTS {stage}')
            raise
        finally:
            if acquired: cur.execute('SELECT RELEASE_LOCK(%s)', (service.lock_name(),))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(migrate(apply=args.apply, backup_dir=args.backup_dir), ensure_ascii=False))
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else '迁移失败，原表保留，请检查连接和权限'
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'message': message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
