"""Minimal triple parsing, isolation and admin access, without database/model calls."""
import json
from pathlib import Path
import pytest
from vendor.chongxi_knowledge_importer import import_knowledge as parser
from services import model_knowledge_import_service as service


def data(items=None):
    return {'question':'问题','dimensions':{name:(items or []) if name=='precautions' else [] for name in parser.DIMENSIONS}}


def test_only_three_content_fields_and_full_conditions_are_preserved():
    item={'content':' 如果出现情况，应观察。加重时就医。 ','topic':' 主题 ','applicable_conditions':['猫','成年'],
          'evidence_status':'supported','source_urls':['https://example.com']}
    question,rows=parser.to_rows(data([item]))
    assert question=='问题'
    assert rows==[{'content':'如果出现情况，应观察。加重时就医。','topic':'主题','applicable_conditions':['猫','成年']}]


def test_exact_triples_deduplicate_within_response_without_merging_models():
    item={'content':'正文','topic':'主题','applicable_conditions':['条件一','条件二']}
    second=dict(item,applicable_conditions=['条件二','条件一'])
    assert len(parser.to_rows(data([item,second]))[1])==1
    assert len(parser.to_rows(data([item,dict(item,topic='另一主题')]))[1])==2


@pytest.mark.parametrize('item',[{'content':''},{'content':'正文','applicable_conditions':'猫'}, {'content':'正文','topic':[]},'正文'])
def test_invalid_items_rejected(item):
    with pytest.raises(ValueError):parser.to_rows(data([item]))


def test_empty_dimensions_are_valid():
    assert parser.to_rows(data())[1]==[]


def test_unknown_dimension_is_not_silently_discarded():
    value=data();value['dimensions']['unknown']=[]
    with pytest.raises(ValueError):parser.to_rows(value)


@pytest.mark.parametrize('name',['csv_labeling','protein_feature_platform','chongxi_knowledge','old_kb','source_db','bad`name','CSV_LABELING'])
def test_database_isolation_rejects_legacy_databases(monkeypatch,name):
    monkeypatch.setattr(service,'get_mysql_config',lambda database=None:{'database':database or 'old_kb'})
    monkeypatch.setenv('MULTI_MODEL_MYSQL_DATABASE','source_db');monkeypatch.setenv('MODEL_KNOWLEDGE_DATABASE',name)
    with pytest.raises(ValueError):service.database_name()


def test_schema_contains_two_tables_with_standardization_json():
    sql=(parser.ROOT/'schema.sql').read_text()
    assert sql.count('CREATE TABLE IF NOT EXISTS')==2
    assert 'knowledge_point_candidate' not in sql and 'knowledge_response_search' not in sql
    body=sql.split('CREATE TABLE IF NOT EXISTS knowledge_point_source_detail (')[1].split(') ENGINE')[0]
    columns=[line.strip().split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith(('KEY','CONSTRAINT'))]
    assert columns==['id','batch_id','source_provider','original_condition','species_code','breed_code','life_stage_codes','need_scene_code','need_scene_name','need_scene_codes','need_relation','scene_meta','need_codes','category_code','topic_code','content','standardization_meta']


def test_import_routes_require_admin(monkeypatch):
    from flask import Flask
    from api.enterprise_api import enterprise_api
    app=Flask(__name__);app.secret_key='tests';app.register_blueprint(enterprise_api)
    client=app.test_client();monkeypatch.setenv('MINIPROGRAM_ADMIN_TOKEN','test-admin')
    assert client.get('/api/admin/model-knowledge').status_code==401
    assert client.post('/api/admin/multi-model/tasks/1/import').status_code==401
    with client.session_transaction() as session:session['workbench_user']={'role':'member'}
    def missing(task_id):raise LookupError('任务不存在')
    monkeypatch.setattr(service,'import_task',missing)
    assert client.post('/api/admin/multi-model/tasks/1/import').status_code==404
