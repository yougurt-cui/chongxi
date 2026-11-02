"""Bounded, resumable two-stage knowledge production, run outside Flask workers."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import time
import uuid

from sqlalchemy import Column, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Session, declarative_base

from services import pet_knowledge_pipeline_service as first
from services import knowledge_second_stage_service as second
from services.knowledge_llm_budget import BudgetExceeded, begin as begin_budget, snapshot as budget_snapshot

Base = declarative_base()
LOCK_NAME = 'chongxi:knowledge-production:v1'


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ScheduleRun(Base):
    __tablename__ = 'knowledge_schedule_runs'
    id = Column(String(32), primary_key=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    status = Column(String(30), nullable=False)
    summary_json = Column(Text, nullable=False, default='{}')


class ScheduleState(Base):
    __tablename__ = 'knowledge_schedule_state'
    name = Column(String(60), primary_key=True)
    post_cursor = Column(Integer, nullable=False, default=0)


class PendingTopic(Base):
    __tablename__ = 'knowledge_schedule_topics'
    topic = Column(String(80), primary_key=True)
    subtopic = Column(String(120), primary_key=True)
    status = Column(String(20), nullable=False, default='pending')


@contextmanager
def job_lock(engine):
    # Named database lock also prevents overlap across hosts. The CLI adds a local file lock.
    with engine.connect() as conn:
        mysql = engine.dialect.name == 'mysql'
        acquired = not mysql or conn.execute(text('SELECT GET_LOCK(:name,0)'), {'name': LOCK_NAME}).scalar() == 1
        try:
            yield acquired
        finally:
            if mysql and acquired:
                conn.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': LOCK_NAME})


def _retry(work, retries):
    for attempt in range(retries + 1):
        try:
            return work()
        except Exception:
            if attempt == retries:
                raise
            time.sleep(min(2 ** attempt, 4))


def _checked(work):
    result = work()
    if result.get('failed_buckets') or result.get('batches', 0) != result.get('batches_done', result.get('batches', 0)):
        raise RuntimeError('Distillation contains failed batches')
    return result


def _save(engine, run_id, summary, status, finished=False):
    with Session(engine) as db:
        row = db.get(ScheduleRun, run_id)
        row.status, row.summary_json = status, json.dumps(summary, ensure_ascii=False)
        if finished:
            row.finished_at = now()
        db.commit()


def _post_topics(engine, pid):
    with engine.connect() as conn:
        return set(tuple(row) for row in conn.execute(text('''SELECT DISTINCT k.topic,k.subtopic
            FROM knowledge_base k JOIN knowledge_sources s ON s.knowledge_id=k.id WHERE s.post_id=:pid'''),
            {'pid': str(pid)}))


def run(max_posts=500, comment_batches=1, chunk=20, retries=1, model=None, verify=True,
        force_reprocess=False, comment_sample_cap=50, max_llm_calls=300,
        max_llm_tokens=1_000_000, usage_audit_path="var/knowledge-llm-usage.jsonl", engine=None):
    """One production pass; unchanged source fingerprints and topic hashes skip model calls.

    Source posts are scanned in rotating ID order, including old posts: new/edited/deleted
    comments on an old post must not be hidden behind an ingest timestamp high-water mark.
    No worker thread is started by importing this module or by Flask.
    """
    if not 1 <= max_posts <= 10000 or not 1 <= comment_batches <= 100 or not 0 <= retries <= 3:
        raise ValueError('Invalid knowledge job limits')
    engine = engine or first._engine()
    begin_budget(max_calls=max_llm_calls, max_tokens=max_llm_tokens, audit_path=usage_audit_path)
    with job_lock(engine) as acquired:
        if not acquired:
            return {'status': 'skipped_locked'}
        first.ensure_tables(engine)
        second.ensure_tables(engine)
        Base.metadata.create_all(engine)
        # Only a process holding the global job lock may mark interrupted runs.
        with Session(engine) as db:
            for old in db.query(ScheduleRun).filter_by(status='running'):
                old.status, old.finished_at = 'interrupted', now()
            state = db.get(ScheduleState, 'default')
            if state is None:
                state = ScheduleState(name='default', post_cursor=0)
                db.add(state)
            cursor = state.post_cursor
            run_id = uuid.uuid4().hex
            db.add(ScheduleRun(id=run_id, started_at=now(), status='running', summary_json='{}'))
            db.commit()
        summary = {'run_id': run_id, 'scanned': 0, 'updated': 0, 'skipped': 0,
                   'topics_updated': 0, 'topics_skipped': 0, 'failures': [], 'post_cursor': cursor}
        changed, blocked = set(), set()
        try:
            with Session(engine) as db:
                posts = first.read_posts(db, cursor, max_posts)
                if not posts and cursor:
                    cursor = 0
                    posts = first.read_posts(db, 0, max_posts)
            for post in posts:
                pid = post['pid']
                previous_topics = _post_topics(engine, pid)
                status, comments = None, None
                try:
                    status, _ = _retry(
                        lambda: first.process_one(engine, post, 'llm', model, force=force_reprocess), retries
                    )
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    # Do not log credentials, request bodies, or source comments in operational output.
                    summary['failures'].append({'stage': 'post_extract', 'post_id': str(pid),
                                                'error_type': type(exc).__name__})
                    blocked.update(previous_topics | _post_topics(engine, pid))
                if status is not None:
                    # Comment sync is tracked separately: post knowledge is already committed
                    # and must not be reported lost when the comment bridge fails.
                    try:
                        comments = _retry(lambda: _checked(lambda: first.run_comment_tasks(
                            str(pid), max_tasks=comment_batches, model=model,
                            sample_cap=comment_sample_cap)), retries)
                    except BudgetExceeded:
                        raise
                    except Exception as exc:
                        summary['failures'].append({'stage': 'comment_sync', 'post_id': str(pid),
                                                    'error_type': type(exc).__name__})
                        # Keep the conservative publication gate: a topic whose comment
                        # evidence is missing stays blocked until comments sync cleanly.
                        blocked.update(previous_topics | _post_topics(engine, pid))
                    topics = previous_topics | _post_topics(engine, pid)
                    if comments is not None and (status == 'updated' or comments.get('batches_done', 0)):
                        changed.update(topics)
                        with Session(engine) as db:
                            for topic, subtopic in topics:
                                pending = db.get(PendingTopic, (topic, subtopic))
                                if pending is None:
                                    db.add(PendingTopic(topic=topic, subtopic=subtopic, status='pending'))
                                else:
                                    pending.status = 'pending'
                            db.commit()
                    summary['updated' if status == 'updated' else 'skipped'] += 1
                summary['scanned'] += 1
                summary['post_cursor'] = int(pid)
                with Session(engine) as db:
                    db.get(ScheduleState, 'default').post_cursor = int(pid)
                    db.commit()
                _save(engine, run_id, summary, 'running')
            if verify:
                with Session(engine) as db:
                    changed.update((r.topic, r.subtopic) for r in db.query(PendingTopic).filter_by(status='pending'))
                for topic, subtopic in sorted(changed - blocked):
                    try:
                        _retry(lambda: _checked(lambda: first.verify_evidence(topic=topic, subtopic=subtopic, model=model)), retries)
                        with Session(engine) as db:
                            db.get(PendingTopic, (topic, subtopic)).status = 'verified'
                            db.commit()
                    except BudgetExceeded:
                        raise
                    except Exception as exc:
                        summary['failures'].append({'stage': 'verify', 'topic': topic, 'subtopic': subtopic,
                                                    'error_type': type(exc).__name__})
                        blocked.add((topic, subtopic))
            # Include old run keys so a topic whose sources were withdrawn is invalidated.
            with engine.connect() as conn:
                groups = set(tuple(row) for row in conn.execute(text('''SELECT topic,subtopic FROM knowledge_base
                    UNION SELECT topic,subtopic FROM kd_topic_runs''')))
            for topic, subtopic in sorted(groups - blocked):
                try:
                    result = _retry(lambda: second.run_distill(topic, subtopic, chunk=chunk, model=model), retries)
                    summary['topics_skipped' if result['skipped'] else 'topics_updated'] += 1
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    summary['failures'].append({'stage': 'second', 'topic': topic, 'subtopic': subtopic,
                                                'error_type': type(exc).__name__})
                _save(engine, run_id, summary, 'running')
            summary['status'] = 'partial_failure' if summary['failures'] else 'completed'
            summary['llm_usage'] = budget_snapshot()
        except Exception as exc:
            summary['status'] = 'failed'
            summary['failures'].append({'stage': 'job', 'error_type': type(exc).__name__})
            summary['llm_usage'] = budget_snapshot()
            _save(engine, run_id, summary, 'failed', finished=True)
            raise
        _save(engine, run_id, summary, summary['status'], finished=True)
        return summary


def recent_runs(limit=10):
    engine = first._engine()
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        return [{'id': r.id, 'status': r.status, 'started_at': r.started_at.isoformat() + 'Z',
                 'finished_at': r.finished_at.isoformat() + 'Z' if r.finished_at else None,
                 'summary': json.loads(r.summary_json)} for r in
                db.query(ScheduleRun).order_by(ScheduleRun.started_at.desc()).limit(max(1, min(limit, 100)))]
