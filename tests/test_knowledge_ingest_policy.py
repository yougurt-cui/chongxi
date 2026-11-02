from datetime import datetime
from unittest.mock import patch
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from services import knowledge_ingest_policy as policy
from services import pet_knowledge_pipeline_service as pipeline
from services import knowledge_schedule_service as schedule


def source_engine():
    engine = create_engine('sqlite://')
    policy.Base.metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(text('CREATE TABLE douyin_raw_content(id INTEGER PRIMARY KEY, aweme_id TEXT, title TEXT, content TEXT)'))
        db.execute(text('CREATE TABLE douyin_raw_comments(id INTEGER PRIMARY KEY, aweme_id TEXT, post_title TEXT, comment_text TEXT)'))
        db.execute(text("INSERT INTO douyin_raw_content VALUES (1,'111','重复标题','旧正文'),(2,'222','重复标题','新正文'),(3,'333','','空标题正文'),(4,NULL,'缺少ID','正文')"))
        db.execute(text("INSERT INTO douyin_raw_comments VALUES (1,'222','重复标题','历史评论'),(2,'111','重复标题','旧帖新评论'),(3,'222','重复标题','新帖子里面的一条有效评论'),(4,'333','','空标题帖子的新评论')"))
    with Session(engine) as db:
        db.add(policy.IngestPolicy(name='douyin', post_floor=1, comment_floor=1, created_at=datetime.now()))
        db.commit()
    return engine


def test_duplicate_and_empty_titles_link_by_platform_id_without_legacy_comments():
    engine = source_engine()
    with Session(engine) as db:
        assert pipeline.read_comments(db, '1', strict_bridge=True) == []
        assert [r['cid'] for r in pipeline.read_comments(db, '2', strict_bridge=True)] == [3]
        assert [r['cid'] for r in pipeline.read_comments(db, '3', strict_bridge=True)] == [4]
        with pytest.raises(ValueError, match='aweme_id'):
            pipeline.read_comments(db, '4', strict_bridge=True)
        assert [r['pid'] for r in pipeline.read_posts(db, 0, 10)] == [2,3,4]
        _, comments = pipeline._evidence_for_posts(db, {1,2,3}, 10)
        assert set(comments) == {2,3}
        assert comments[2][0]['comment_id'] == 3


def test_manual_legacy_processing_skips_without_model_calls():
    engine = source_engine()
    pipeline.ensure_tables(engine)
    with patch.object(pipeline, '_engine', return_value=engine), patch.object(pipeline, '_qwen') as qwen:
        assert pipeline.process_one(engine, {'pid':1}, 'llm') == ('skipped',0)
        assert pipeline.run_comment_tasks('1')['status'] == 'legacy_skipped'
        qwen.assert_not_called()


def test_schedule_empty_new_queue_does_not_distill_historical_topics():
    engine = source_engine()
    pipeline.ensure_tables(engine)
    schedule.Base.metadata.create_all(engine)
    with Session(engine) as db:
        row = db.get(policy.IngestPolicy, 'douyin'); row.post_floor = 4
        db.add(schedule.ScheduleState(name='default',post_cursor=0))
        db.commit()
    with patch.object(schedule.first, '_qwen'), patch.object(schedule.first,'verify_evidence') as verify, patch.object(schedule.second,'run_distill') as distill:
        r = schedule.run(engine=engine)
    assert r['scanned'] == 0
    assert r['post_cursor'] == 4
    verify.assert_not_called(); distill.assert_not_called()


def test_import_keeps_different_posts_with_identical_titles_and_comments(tmp_path):
    import csv
    from vendor.csv_mysql_labeling.scripts.generate_douyin_raw_comments_sql import load_records
    p = tmp_path / 'search_comments_ids.csv'
    with p.open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=['aweme_id','标题','内容','评论'])
        w.writeheader()
        for aid in ['1234567890123456789','2234567890123456789']:
            w.writerow({'aweme_id':aid,'标题':'相同标题','内容':'正文','评论':'同一句评论'})
    rows=load_records(tmp_path)
    assert len(rows) == 2
    assert rows[0]['external_id'] != rows[1]['external_id']
    assert rows[0]['aweme_id'] == '1234567890123456789'


def test_import_rejects_missing_platform_id_without_archiving(tmp_path):
    import csv
    from adapters import comment_data_adapter as adapter
    with (tmp_path / 'search_comments_missing.csv').open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=['标题','内容','评论']);w.writeheader()
        w.writerow({'标题':'标题','内容':'正文','评论':'缺少关联ID'})
    with patch.object(adapter, 'ensure_douyin_raw_comments_table'), patch.object(adapter, 'archive_files') as archive:
        with pytest.raises(ValueError, match='aweme_id'):
            adapter.ingest_douyin_comments(engine=None, data_dir=tmp_path)
        archive.assert_not_called()


def test_cutover_is_idempotent_and_preserves_completed_results(tmp_path):
    from scripts.cutover_knowledge_ingestion import cutover
    engine = source_engine()
    with engine.begin() as db:
        db.execute(text('DELETE FROM knowledge_ingest_policy'))
    pipeline.ensure_tables(engine)
    schedule.Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(pipeline.Task(post_id='1',content_hash='existing',status='completed',attempts=1))
        db.add(pipeline.Task(post_id='2',content_hash='',status='failed',attempts=1))
        db.add(schedule.ScheduleState(name='default',post_cursor=2))
        db.add(schedule.PendingTopic(topic='日常喂养',subtopic='其他',status='pending'))
        db.commit()
    preview = cutover(engine)
    assert preview['status'] == 'dry_run'
    assert policy.for_engine(engine) is None
    result = cutover(engine, apply=True, backup_dir=tmp_path)
    assert result['posts']['max_id'] == 4
    assert result['comments']['max_id'] == 4
    with Session(engine) as db:
        states={r.post_id:r.status for r in db.query(pipeline.Task)}
        assert states == {'1':'completed','2':'legacy_skipped'}
        assert db.get(schedule.ScheduleState,'default').post_cursor == 4
        assert db.query(schedule.PendingTopic).one().status == 'legacy_skipped'
    assert cutover(engine, apply=True, backup_dir=tmp_path)['status'] == 'already_applied'
    assert len(list(tmp_path.glob('*.json'))) == 1
