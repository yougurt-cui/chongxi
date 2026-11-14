"""Scene fields are required; unrelated additive fields must not break task reads."""
from unittest.mock import MagicMock
import pytest
from services import model_knowledge_import_service as store

FIELDS={'id','batch_id','source_provider','original_condition','species_code','breed_code',
        'life_stage_codes','need_scene_code','need_scene_name','need_scene_codes','need_relation','scene_meta',
        'need_codes','category_code','topic_code','content','standardization_meta'}


def run_schema_check(monkeypatch,fields):
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value;cur.__enter__.return_value=cur
    cur.fetchone.return_value={'Field':'knowledge_base'}
    cur.fetchall.return_value=[{'Field':name} for name in fields]
    monkeypatch.setattr(store,'get_mysql_config',lambda database=None:{'database':database})
    monkeypatch.setattr(store,'connect',lambda:conn)
    monkeypatch.setattr('pymysql.connect',lambda **kwargs:conn)
    store._ensure_database.__wrapped__('scene_schema_unit_test')


def test_scene_columns_and_future_additive_fields_are_accepted(monkeypatch):
    run_schema_check(monkeypatch,FIELDS|{'optional_future_field'})


def test_missing_required_scene_column_is_rejected(monkeypatch):
    with pytest.raises(RuntimeError,match='字段'):
        run_schema_check(monkeypatch,FIELDS-{'need_scene_code'})
