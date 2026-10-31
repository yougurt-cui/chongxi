import unittest
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from services import knowledge_schedule_service as job
from services import knowledge_second_stage_service as second


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        second.Base.metadata.create_all(self.engine)
        with self.engine.begin() as c:
            c.execute(text('CREATE TABLE knowledge_base(id INTEGER PRIMARY KEY,topic TEXT,subtopic TEXT,content TEXT,evidence_status TEXT,status TEXT)'))
            c.execute(text('CREATE TABLE knowledge_sources(knowledge_id INTEGER,post_id TEXT)'))
            c.execute(text("INSERT INTO knowledge_base VALUES(1,'行为','抓挠','猫抓沙发','待验证','待审核')"))
            c.execute(text("INSERT INTO knowledge_sources VALUES(1,'1')"))
        self.cursor_calls = []

    def tearDown(self):
        self.engine.dispose()

    def read(self, db, cursor, size):
        self.cursor_calls.append(cursor)
        return [{'pid': 1}] if cursor == 0 else []

    def execute(self, **kwargs):
        with patch.object(job.first, 'ensure_tables'), patch.object(job.first, 'read_posts', side_effect=self.read), \
             patch.object(job.first, 'process_one', return_value=('updated', 1)), \
             patch.object(job.first, 'run_comment_tasks', return_value={'batches': 1, 'batches_done': 1}), \
             patch.object(job.first, 'verify_evidence', return_value={'failed_buckets': []}) as verify, \
             patch.object(job.second, 'run_distill', return_value={'skipped': False}) as distill:
            result = job.run(engine=self.engine, retries=0, **kwargs)
        return result, verify, distill

    def test_rotating_scan_revisits_old_posts_and_checkpoints(self):
        result, verify, distill = self.execute()
        self.assertEqual('completed', result['status'])
        self.assertEqual(1, result['post_cursor'])
        verify.assert_called_once_with(topic='行为', subtopic='抓挠', model=None)
        distill.assert_called_once()
        self.execute()
        self.assertEqual([0, 1, 0], self.cursor_calls)  # old comments are scanned again
        with Session(self.engine) as db:
            self.assertEqual(2, db.query(job.ScheduleRun).filter_by(status='completed').count())

    def test_failed_comments_do_not_publish_partial_topic_and_failure_is_persisted(self):
        with patch.object(job.first, 'ensure_tables'), patch.object(job.first, 'read_posts', side_effect=self.read), \
             patch.object(job.first, 'process_one', return_value=('updated', 1)), \
             patch.object(job.first, 'run_comment_tasks', return_value={'batches': 1, 'batches_done': 0}), \
             patch.object(job.second, 'run_distill') as distill:
            result = job.run(engine=self.engine, retries=0)
        self.assertEqual('partial_failure', result['status'])
        distill.assert_not_called()
        with Session(self.engine) as db:
            self.assertEqual('partial_failure', db.get(job.ScheduleRun, result['run_id']).status)

    def test_lock_contention_skips_before_any_schema_or_model_work(self):
        @contextmanager
        def unavailable(_):
            yield False
        with patch.object(job, 'job_lock', unavailable), patch.object(job.first, 'ensure_tables') as ensure:
            self.assertEqual('skipped_locked', job.run(engine=self.engine)['status'])
        ensure.assert_not_called()

    def test_no_source_withdraws_old_guide_and_repeated_run_is_idempotent(self):
        with Session(self.engine) as db:
            db.add(second.KdGuide(topic='旧主题', subtopic='旧子题', problem_key='old', problem='旧问题',
                                  content_hash='old', source_ids_json='[999]'))
            db.commit()
        with patch.object(second, '_engine', return_value=self.engine):
            first = second.run_distill('旧主题', '旧子题', mode='demo')
            repeat = second.run_distill('旧主题', '旧子题', mode='demo')
        self.assertEqual(1, first['invalidated'])
        self.assertTrue(repeat['skipped'])
        with Session(self.engine) as db:
            self.assertEqual('无有效来源', db.query(second.KdGuide).filter_by(problem_key='old').one().review_status)

    def test_invalid_limits_are_rejected_before_database_work(self):
        for options in ({'max_posts': 0}, {'comment_batches': 0}, {'retries': -1}):
            with self.assertRaises(ValueError):
                job.run(engine=self.engine, **options)

    def test_force_reprocess_is_explicitly_forwarded(self):
        with patch.object(job.first, 'ensure_tables'), patch.object(job.first, 'read_posts', side_effect=self.read), \
             patch.object(job.first, 'process_one', return_value=('skipped', 0)) as process, \
             patch.object(job.first, 'run_comment_tasks', return_value={'batches': 0, 'batches_done': 0}), \
             patch.object(job.first, 'verify_evidence', return_value={'failed_buckets': []}), \
             patch.object(job.second, 'run_distill', return_value={'skipped': True}):
            job.run(engine=self.engine, retries=0, force_reprocess=True)
        process.assert_called_once_with(self.engine, {'pid': 1}, 'llm', None, force=True)

    def test_failed_verification_stays_pending_until_a_later_success(self):
        with patch.object(job.first, 'ensure_tables'), patch.object(job.first, 'read_posts', side_effect=self.read), \
             patch.object(job.first, 'process_one', return_value=('updated', 1)), \
             patch.object(job.first, 'run_comment_tasks', return_value={'batches': 0, 'batches_done': 0}), \
             patch.object(job.first, 'verify_evidence', return_value={'failed_buckets': ['failed']}), \
             patch.object(job.second, 'run_distill') as distill:
            result = job.run(engine=self.engine, retries=0)
        self.assertEqual('partial_failure', result['status'])
        distill.assert_not_called()
        with Session(self.engine) as db:
            self.assertEqual('pending', db.get(job.PendingTopic, ('行为', '抓挠')).status)
        with patch.object(job.first, 'ensure_tables'), patch.object(job.first, 'read_posts', side_effect=self.read), \
             patch.object(job.first, 'process_one', return_value=('skipped', 0)), \
             patch.object(job.first, 'run_comment_tasks', return_value={'batches': 0, 'batches_done': 0}), \
             patch.object(job.first, 'verify_evidence', return_value={'failed_buckets': []}) as verify, \
             patch.object(job.second, 'run_distill', return_value={'skipped': False}):
            job.run(engine=self.engine, retries=0)
        verify.assert_called_once()
