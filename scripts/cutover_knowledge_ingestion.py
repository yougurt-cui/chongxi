#!/usr/bin/env python3
"""Skip the existing corpus once, preserving completed results and rollback metadata."""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import inspect, text
from services import knowledge_ingest_policy as policy
from services import knowledge_schedule_service as schedule
from services import pet_knowledge_pipeline_service as pipeline


def cutover(engine, *, apply=False, backup_dir='var/deploy_backups'):
    with schedule.job_lock(engine) as acquired:
        if not acquired:
            raise RuntimeError('知识任务正在运行，切换未执行')
        existing = policy.for_engine(engine)
        if existing:
            return {'status': 'already_applied', **existing}
        if apply:
            policy.Base.metadata.create_all(engine)
            cols = {r['name'] for r in inspect(engine).get_columns('douyin_raw_comments')}
            if 'aweme_id' not in cols:
                with engine.begin() as db:
                    db.execute(text('ALTER TABLE douyin_raw_comments ADD COLUMN aweme_id VARCHAR(64) NULL, ADD INDEX idx_comment_aweme_id (aweme_id)'))
        with engine.begin() as db:
            posts = dict(db.execute(text('SELECT COUNT(*) AS n,COALESCE(MAX(id),0) AS max_id FROM douyin_raw_content')).mappings().one())
            comments = dict(db.execute(text('SELECT COUNT(*) AS n,COALESCE(MAX(id),0) AS max_id FROM douyin_raw_comments')).mappings().one())
            snapshot = {'posts': posts, 'comments': comments, 'created_at': datetime.now(timezone.utc).isoformat()}
            for table, columns in [
                ('knowledge_schedule_state','name,post_cursor'),
                ('knowledge_schedule_topics','topic,subtopic,status'),
                ('knowledge_tasks','id,status'),
                ('knowledge_comment_items','id,status')]:
                snapshot[table] = [dict(row) for row in db.execute(text(f'SELECT {columns} FROM {table}')).mappings()]
            if not apply:
                return {'status': 'dry_run', 'posts': posts, 'comments': comments}
            root = Path(backup_dir); root.mkdir(parents=True, exist_ok=True)
            path = root / ('knowledge-cutover-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
            with path.open('x') as f:
                os.chmod(path, 0o600)
                json.dump(snapshot, f, ensure_ascii=False, default=str)
            db.execute(policy.IngestPolicy.__table__.insert().values(name='douyin', post_floor=posts['max_id'],
                comment_floor=comments['max_id'], status='legacy_skipped'))
            db.execute(text("UPDATE knowledge_tasks SET status='legacy_skipped' WHERE CAST(post_id AS UNSIGNED)<=:floor AND status<>'completed'"), {'floor':posts['max_id']})
            db.execute(text("UPDATE knowledge_comment_items SET status='legacy_skipped' WHERE CAST(post_id AS UNSIGNED)<=:floor AND status IN ('pending','failed')"), {'floor':posts['max_id']})
            db.execute(text("UPDATE knowledge_schedule_topics SET status='legacy_skipped' WHERE status IN ('pending','verified')"))
            db.execute(text("UPDATE knowledge_schedule_state SET post_cursor=:floor WHERE name='default'"), {'floor':posts['max_id']})
        return {'status':'applied', 'posts':posts, 'comments':comments, 'backup':str(path)}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args=parser.parse_args()
    print(json.dumps(cutover(pipeline._engine(), apply=args.apply), ensure_ascii=False))
