"""Opt-in MySQL 8 migration/import tests using a disposable copy of solution data."""
import json
import os
from pathlib import Path
import uuid

import pymysql
import pytest

from app_config import get_mysql_config
from services import model_knowledge_import_service as store
from services import solution_knowledge_service as solution
from services import solution_scene_service as scenes
from scripts.migrate_solution_scene_grain import migrate, snapshot
from scripts.remove_solution_source_detail import migrate as remove_detail

pytestmark = pytest.mark.skipif(os.getenv('SOLUTION_SCENE_MYSQL_TEST') != '1', reason='requires disposable MySQL database')


@pytest.fixture
def database(monkeypatch):
    cfg = get_mysql_config(database='chongxi_model_knowledge')
    cfg.update(cursorclass=pymysql.cursors.DictCursor, connect_timeout=10, read_timeout=30)
    name = 'test_solution_scene_'+uuid.uuid4().hex[:16]
    admin = pymysql.connect(**cfg)
    with admin.cursor() as cur:
        cur.execute('SHOW COLUMNS FROM solution_knowledge_point')
        legacy = 'batch_id' in {r['Field'] for r in cur.fetchall()}
        original = 'solution_knowledge_point' if legacy else 'solution_knowledge_point_legacy_scene_v1'
        cur.execute('CREATE DATABASE `'+name+'` CHARACTER SET utf8mb4')
        for target, source in [('knowledge_import_batch','knowledge_import_batch'),
                               ('solution_knowledge_point',original), ('health_solution','health_solution')]:
            cur.execute('CREATE TABLE `'+name+'`.`'+target+'` LIKE `chongxi_model_knowledge`.`'+source+'`')
            condition = " WHERE knowledge_base='solution'" if target=='knowledge_import_batch' else ''
            cur.execute('INSERT INTO `'+name+'`.`'+target+'` SELECT * FROM `chongxi_model_knowledge`.`'+source+'`'+condition)
    with admin.cursor() as cur:
        for table in ("health_knowledge_point_detail", "knowledge_point_source_detail", "daily_care_knowledge_point_detail", "behavior_knowledge_point_detail"):
            cur.execute("CREATE TABLE `"+name+"`.`"+table+"` (id BIGINT,batch_id BIGINT)")
    with admin.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM `"+name+"`.knowledge_import_batch LIKE 'solution_parse_result'")
        if cur.fetchone():cur.execute('UPDATE `'+name+'`.knowledge_import_batch SET solution_parse_result=NULL')
    admin.commit()
    test_cfg={**cfg,'database':name}
    monkeypatch.setattr(store,'connect',lambda: pymysql.connect(**test_cfg))
    monkeypatch.setattr(store,'database_name',lambda: name)
    monkeypatch.setattr(store,'ensure_database',lambda: name)
    try:
        yield name
    finally:
        with admin.cursor() as cur:
            cur.execute('DROP DATABASE `'+name+'`')
        admin.close()


def test_migration_lossless_idempotent_import_and_cross_model_merge(database, tmp_path):
    with store.connect() as conn:
        with conn.cursor() as cur:
            before=snapshot(cur,'solution_knowledge_point')['rows']
        preview=migrate(conn)
        assert preview['source_rows']==len(before)
        assert preview['scene_rows']==15
        result=migrate(conn,apply=True,backup_dir=tmp_path/'backup')
        assert Path(result['backup']).exists()
        assert Path(result['backup']).with_name('restore.sql').exists()
        with conn.cursor() as cur:
            assert snapshot(cur,'solution_knowledge_point_detail')['rows']==before
            cur.execute('SELECT * FROM solution_knowledge_point ORDER BY id')
            points=list(cur.fetchall())
            assert len(points)==15
            assert {r['need_code'] for r in points}=={'fecal_odor_management'}
            assert all(not r['scenario_code'].startswith('legacy_') for r in points)
            assert all(isinstance(store.decode(r['verification']),dict) for r in points)
            assert sum(r['source_count'] for r in points)==len(before)
            ids={tuple(r[k] for k in scenes.GRAIN):r['id'] for r in points}
            cur.execute("SELECT id FROM knowledge_import_batch WHERE knowledge_base='solution' ORDER BY id")
            bids=[r['id'] for r in cur.fetchall()]
    remove_detail(apply=True,backup_root=tmp_path/'removed-detail')
    for bid in bids:
        assert solution.import_batch(bid)['status']=='already_imported'
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT * FROM solution_knowledge_point ORDER BY id')
        after=list(cur.fetchall())
        assert {tuple(r[k] for k in scenes.GRAIN):r['id'] for r in after}==ids
        cur.execute('SELECT * FROM knowledge_import_batch WHERE id=%s',(bids[0],))
        batch=cur.fetchone()
        batch.update(id=1000001, task_id=1000001, provider='test_model', imported_at=None,solution_parse_result=None)
        cur.execute('INSERT INTO knowledge_import_batch ('+','.join(batch)+') VALUES ('+','.join(['%s']*len(batch))+')',tuple(batch.values()))
        conn.commit()
    assert solution.import_batch(1000001)['inserted_count']==5
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT * FROM solution_knowledge_point ORDER BY id')
        points=list(cur.fetchall())
        assert len(points)==15
        assert sum(r['source_count']==2 for r in points)==5
        assert sum(r['model_count']==2 for r in points)==5
        cur.execute('SELECT task_id FROM knowledge_import_batch WHERE id=%s',(bids[0],))
        task_id=cur.fetchone()['task_id']
    packet=solution.task_result(task_id)
    assert len(packet['points'])==15 and len(packet['source_points'])==15
    assert len(scenes.task_points(1000001))==5
    from services.multi_model_knowledge_service import list_tasks
    listed=list_tasks(knowledge_base='solution')
    by_task={r['id']:r for r in listed['items']}
    assert by_task[task_id]['point_count']==15
    assert by_task[1000001]['point_count']==5


def test_reviewed_scene_conflict_rolls_back_detail_replacement(database,tmp_path):
    with store.connect() as conn:
        migrate(conn,apply=True,backup_dir=tmp_path/'backup')
        conn.commit()
        remove_detail(apply=True,backup_root=tmp_path/'removed-detail')
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM solution_knowledge_point ORDER BY id LIMIT 1')
            point=cur.fetchone()
            bid=store.decode(point['sources'])[0]['batch_id']
            cur.execute("UPDATE solution_knowledge_point SET review_status='verified' WHERE id=%s",(point['id'],))
            cur.execute('SELECT parsed_response FROM knowledge_import_batch WHERE id=%s',(bid,))
            doc=store.decode(cur.fetchone()['parsed_response'])
            # Modify the exact scene referenced by the reviewed main row.
            raw=store.decode(point['sources'])[0]['scenario_data']
            candidate=next(p for p in doc['solutions'] if p['scenario']==raw['scenario'])
            candidate['solution'][0]='测试变更，不应覆盖已审核方案'
            cur.execute('UPDATE knowledge_import_batch SET parsed_response=%s WHERE id=%s',(store.dumps(doc),bid))
            cur.execute('SELECT solution_parse_result FROM knowledge_import_batch WHERE id=%s',(bid,))
            before=cur.fetchone()['solution_parse_result']
        conn.commit()
    with pytest.raises(ValueError,match='人工审核'):
        solution.import_batch(bid)
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT solution_parse_result FROM knowledge_import_batch WHERE id=%s',(bid,))
        assert cur.fetchone()['solution_parse_result']==before
        cur.execute('SELECT review_status,input_hash FROM solution_knowledge_point WHERE id=%s',(point['id'],))
        row=cur.fetchone()
        assert row=={'review_status':'verified','input_hash':point['input_hash']}


def test_fresh_schema_and_import(database):
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('DROP TABLE solution_knowledge_point')  # disposable test DB only
        solution.ensure_schema(cur)
        cur.execute("SELECT id FROM knowledge_import_batch WHERE knowledge_base='solution' ORDER BY id")
        bids=[r['id'] for r in cur.fetchall()]
        conn.commit()
    for bid in bids:
        solution.import_batch(bid)
    with store.connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) n FROM solution_knowledge_point')
        assert cur.fetchone()['n']==15
        cur.execute("SELECT SUM(JSON_LENGTH(JSON_EXTRACT(solution_parse_result,'$.points'))) n FROM knowledge_import_batch WHERE knowledge_base='solution'")
        assert cur.fetchone()['n']==15
        cur.execute("SHOW TABLES LIKE 'solution_knowledge_point_detail'")
        assert not cur.fetchone()
