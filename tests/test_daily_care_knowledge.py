import copy
import json
from unittest.mock import MagicMock,patch
import pytest
from flask import Flask
from api.enterprise_api import enterprise_api
from services import daily_care_knowledge_service as care,model_knowledge_import_service as store
from services import multi_model_prompt_service as prompts,multi_model_knowledge_service as multi
from vendor.chongxi_multi_model_mysql import run


def document():
    return {'knowledge_base':'daily_care','question':'成年长毛猫怎么梳毛','care_points':[{
        'species':'cat','breed':'未限定','life_stage':'adult','care_code':'coat_brushing','care_name':'日常梳毛',
        'category_code':'grooming','applicable_conditions':['愿意接受接触的成年猫'],'frequency':'根据毛发状态与接受程度调整',
        'steps':[{'action':'短时轻柔梳理','reason':'观察接受程度'}],'observation_fields':['皮肤状态'],
        'precautions':['避开不适部位'],'stop_conditions':['疼痛或皮肤异常时停止并咨询兽医'],
        'source_urls':[],'evidence_status':'pending_verification'}]}


def db():
    conn=MagicMock();conn.__enter__.return_value=conn;cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value={'id':5,'knowledge_base':'daily_care','status':'success','question':document()['question'],'raw_response':json.dumps(document())}
    cur.fetchall.return_value=[]
    return conn,cur


def test_templates_and_collector_contract():
    base=next(b for b in prompts.collection_bases()['bases'] if b['code']=='daily_care')
    assert len(base['templates'])==2
    for t in base['templates']:assert '{{question}}' in t['template'] and 'care_points' in t['template']
    doc=document();doc['care_points']*=2
    assert run.extract_json(json.dumps(doc),knowledge_base='daily_care')==document()
    assert run.count_points(document())==1


@pytest.mark.parametrize('field,value',[('category_code','disease'),('species','bird'),('steps',[]),('steps',['unsafe']),('stop_conditions',[]),('source_urls',['javascript:alert(1)']),('evidence_status','verified')])
def test_invalid_points_rejected(field,value):
    doc=document();doc['care_points'][0][field]=value
    with pytest.raises(ValueError):care.validate_document(doc)


def test_import_is_independent_and_retry_preserves_review():
    conn,cur=db()
    dictionary={'species':[{'species_code':'cat'}],'breeds':[{'species_code':'cat','breed_code':'unknown_cat','breed_name':'未知'}],'stages':[{'species_code':'cat','stage_code':'adult'}]}
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn),patch.object(care,'load_dictionary',return_value=dictionary),patch.object(run,'call_provider') as model:
        assert care.import_batch(5)['inserted_count']==1
        insert=next(c.args for c in cur.execute.call_args_list if c.args[0].startswith('INSERT'))
        assert 'daily_care_knowledge_point_detail' in insert[0] and 'unknown_cat' in insert[1]
        key=__import__('hashlib').sha256(store.dumps(document()['care_points'][0]).encode()).hexdigest()
        cur.reset_mock();cur.fetchall.return_value=[{'point_key':key,'review_status':'verified'}]
        assert care.import_batch(5)['status']=='already_imported'
        assert not any(c.args[0].startswith(('DELETE','INSERT')) for c in cur.execute.call_args_list)
        cur.fetchall.return_value=[{'point_key':'old','review_status':'verified'}]
        with pytest.raises(ValueError,match='人工审核'):care.import_batch(5)
        model.assert_not_called()


def test_success_is_saved_before_auto_import_and_api_failures_skip_import():
    conn,cur=db()
    result={'provider':'qwen','status':'success','raw_text':json.dumps(document()),'parsed':None,'error':None,'latency_ms':1}
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn),patch.object(store,'process_batch_pipeline',side_effect=lambda _:conn.commit.assert_called_once()) as process:
        store.persist_model_result(1,result)
    process.assert_called_once_with(5)
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn),patch.object(store,'process_batch_pipeline') as process:
        store.persist_model_result(1,{**result,'status':'api_failed'})
    process.assert_not_called()


def test_search_and_authentication():
    conn,cur=db();cur.fetchone.return_value={'n':0}
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn):
        assert care.list_points(query='梳毛%_',species='cat',category='grooming')['total']==0
    sql,params=cur.execute.call_args.args
    assert '梳毛' not in sql and '%梳毛!%!_%' in params
    app=Flask(__name__);app.secret_key='test';app.register_blueprint(enterprise_api);client=app.test_client()
    with patch.dict('os.environ',{'MINIPROGRAM_ADMIN_TOKEN':'test'}):assert client.get('/api/admin/multi-model/daily-care-points').status_code==401
    with client.session_transaction() as session:session['workbench_user']={'role':'data_admin'}
    assert client.get('/api/admin/multi-model/daily-care-points?species=bird').status_code==400
    with patch.object(care,'list_points',return_value={'items':[],'total':0}):assert client.get('/api/admin/multi-model/daily-care-points').get_json()['ok']


def test_daily_care_dispatch_and_failed_json_retains_successful_call():
    conn,cur=db()
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn),patch.object(care,'import_batch',return_value={'status':'completed'}) as importer:
        assert store.import_batch(5)['status']=='completed'
    importer.assert_called_once_with(5)
    provider=run.Provider('qwen','test','https://example.com','test')
    with patch('services.model_native_search_service.call',return_value=('not JSON',{},{})),patch.object(run,'extract_json') as parse:
        r=run.call_provider(provider,'prompt',web_search=True,knowledge_base='daily_care')
    assert r['status']=='success' and r['parsed'] is None
    parse.assert_not_called()



def test_incomplete_response_is_reported_without_parsing_or_touching_points():
    conn,cur=db();cur.fetchone.return_value.update(search_metadata=json.dumps({'response_status':'incomplete'}))
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn),patch.object(care,'load_dictionary',return_value={}),patch.object(run,'extract_json') as extract:
        with pytest.raises(ValueError,match='回答被截断'):care.import_batch(5)
    extract.assert_not_called()
    assert not any('INSERT' in c.args[0] or 'DELETE' in c.args[0] for c in cur.execute.call_args_list)
