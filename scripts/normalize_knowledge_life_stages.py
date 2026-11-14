#!/usr/bin/env python3
"""Back up and replace the exact legacy [unspecified] life-stage singleton."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services import model_knowledge_import_service as store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup_file', type=Path)
    args = parser.parse_args()
    with store.connect() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM knowledge_point_source ORDER BY id FOR UPDATE')
                rows = list(cur.fetchall())
                affected = [r for r in rows if store.decode(r['life_stage_codes']) == ['unspecified']]
                with args.backup_file.open('x', encoding='utf-8') as backup:
                    os.chmod(args.backup_file, 0o600)
                    json.dump({'database': store.database_name(), 'table': 'knowledge_point_source',
                               'rows': affected}, backup, ensure_ascii=False, default=str, indent=2)
                    backup.flush()
                    os.fsync(backup.fileno())
                for row in affected:
                    cur.execute('UPDATE knowledge_point_source SET life_stage_codes=%s WHERE id=%s',
                                (store.dumps(['all_stages']), row['id']))
                cur.execute('SELECT * FROM knowledge_point_source ORDER BY id')
                after = list(cur.fetchall())
                expected = [dict(r, life_stage_codes=store.dumps(['all_stages']))
                            if store.decode(r['life_stage_codes']) == ['unspecified'] else r for r in rows]
                assert after == expected, 'Unexpected changes; transaction will roll back'
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    print(json.dumps({'database': store.database_name(), 'updated': len(affected),
                      'remaining_unspecified': 0, 'backup': str(args.backup_file)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
