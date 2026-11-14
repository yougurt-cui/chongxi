import json
from copy import deepcopy
from unittest.mock import MagicMock, patch
import pytest
from services import model_knowledge_import_service as store
from services import product_recommendation_import_service as products
from services import knowledge_source_assembly_service as assembly


def detail():
    return dict(id=71, batch_id=5, source_provider='qwen', source_model='qwen-plus',
        species_code='cat', breed_code='ragdoll', life_stage_codes=['adult'],
        need_scene_code='food_selection', need_scene_name='选粮', need_scene_codes=['food_selection'],
        need_relation='direct', need_codes=['coat_health'], original_condition='成年布偶猫',
        category_code='product_goods', topic_code='product_recommendation', content='渴望；六种鱼猫粮；支持毛发',
        standardization_meta={'status':'success','raw_input':{'source_context':{
            'question':'布偶吃什么','structured_item':{'brand':'渴望','product_name':'六种鱼猫粮',
                'reason':'支持毛发','applicable_conditions':['成年猫']}}}})


def assembled(rows):
    groups,_,_=assembly.category_groups(rows)
    return [dict(zip(assembly.KEY_COLUMNS,key),id=i+1,sources=deepcopy(entries))
            for i,(key,entries) in enumerate(groups.items())]


def connection(rows=None, batch=None):
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=rows or []
    cur.fetchone.side_effect=[{'acquired':1},batch] if batch else None
    return conn,cur


def test_batch_filter_excludes_other_models_and_keeps_source_scope():
    row=detail();other={**deepcopy(row),'id':72,'batch_id':6,'source_provider':'doubao'}
    selected=products.batch_source_groups(5,[row],assembled([row,other]))
    result=products.normalize(selected)
    assert not result['errors'] and len(result['records'])==1
    record=result['records'][0]
    assert record['sources'][0]['batch_id']==5 and record['original_source_id']==71
    assert record['recommendations'][0]['life_stage_code']=='adult'


@pytest.mark.parametrize('change',['missing','stale','pending'])
def test_incomplete_or_stale_assembly_never_reaches_import(change):
    row=detail();rows=assembled([row])
    if change=='missing':rows=[]
    if change=='stale':rows[0]['sources'][0]['sources'][0]['source_context']['structured_item']['reason']='旧理由'
    if change=='pending':row['standardization_meta']['status']='pending'
    with pytest.raises(ValueError):products.batch_source_groups(5,[row],rows)


def test_batch_import_writes_only_this_batch_and_reports_counts():
    row=detail();conn,_=connection([row])
    with patch.object(store,'connect',return_value=conn),patch.object(products,'read_sources',return_value=assembled([row])), \
         patch.object(products,'import_records',return_value={'imported':1,'unchanged':0}) as write:
        result=products.import_batch_recommendations(5)
    assert result['status']=='completed' and result['groups']==1
    assert result['source_records']==1 and write.call_args.args[0][0]['original_source_id']==71


def test_no_products_skips_without_initializing_product_database():
    conn,_=connection([])
    with patch.object(store,'connect',return_value=conn),patch.object(products,'read_sources',return_value=[]), \
         patch.object(products,'import_records') as write:
        assert products.import_batch_recommendations(5)['status']=='skipped'
    write.assert_not_called()


def test_selection_pipeline_order_and_retry_share_the_same_execution():
    conn,_=connection(batch={'knowledge_base':'selection'});events=[]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'_product_status') as status, \
         patch.object(store,'import_batch',side_effect=lambda bid: events.append('raw') or {'status':'already_imported'}), \
         patch('services.knowledge_standardization_service.normalize',side_effect=lambda **kw: events.append('mapping_and_assembly') or {}), \
         patch.object(products,'import_batch_recommendations',side_effect=lambda bid: events.append('product') or {'status':'completed'}):
        result=store.process_batch_pipeline(5)
    assert events==['raw','mapping_and_assembly','product']
    assert result['product_import']['status']=='completed'
    assert status.call_args.args[1]['status']=='completed'


@pytest.mark.parametrize('normalized',[{'failed':1},{'busy':1},{'changed':1},{'status':'paused'}])
def test_failed_or_paused_mapping_preserves_error_and_never_imports_products(normalized):
    conn,_=connection(batch={'knowledge_base':'selection'})
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'_product_status') as status,patch.object(store,'import_batch',return_value={}), \
         patch('services.knowledge_standardization_service.normalize',return_value=normalized), \
         patch.object(products,'import_batch_recommendations') as write:
        with pytest.raises(ValueError):store.process_batch_pipeline(5)
    write.assert_not_called();assert status.call_args.args[1]['status']=='failed'


def test_health_pipeline_keeps_original_handler_and_does_not_run_product_logic():
    conn,_=connection(batch={'knowledge_base':'health'})
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'import_batch',return_value={'health_status':'completed'}) as health, \
         patch.object(store,'_product_status') as status,patch.object(products,'import_batch_recommendations') as write:
        assert store.process_batch_pipeline(5)['health_status']=='completed'
    health.assert_called_once_with(5);status.assert_not_called();write.assert_not_called()


def test_product_database_failure_is_reported_without_losing_saved_response():
    conn,_=connection(batch={'knowledge_base':'selection'})
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'import_batch',return_value={}),patch.object(store,'_product_status') as status, \
         patch('services.knowledge_standardization_service.normalize',return_value={}), \
         patch.object(products,'import_batch_recommendations',side_effect=RuntimeError('private connection')):
        with pytest.raises(RuntimeError):store.process_batch_pipeline(5)
    assert status.call_args.args[1]['status']=='failed'
    assert 'private connection' not in status.call_args.args[1]['message']


@pytest.mark.parametrize('response_status',['success','api_failed'])
def test_automatic_collection_calls_shared_pipeline_only_for_success(response_status):
    conn,cur=connection();cur.fetchone.return_value={'id':5}
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'process_batch_pipeline') as pipeline:
        store.persist_model_result(1, {'provider':'qwen','status':response_status,'raw_text':'original',
            'parsed':{} if response_status=='success' else None,'error':None,'latency_ms':10})
    assert pipeline.call_count == (1 if response_status=='success' else 0)
    assert any('raw_response=%s' in call.args[0] for call in cur.execute.call_args_list)


def test_manual_retry_uses_same_pipeline_and_reports_per_batch_failure():
    conn,_=connection([{'id':5,'status':'success'},{'id':6,'status':'success'},{'id':7,'status':'api_failed'}])
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'process_batch_pipeline',side_effect=[{'batch_id':5,'product_import':{'status':'completed'}},ValueError('blocked')]) as pipeline:
        result=store.import_task(1)
    assert [call.args[0] for call in pipeline.call_args_list]==[5,6]
    assert result['status']=='partial_failed' and result['errors'][0]['batch_id']==6


def test_task_view_is_read_only_scoped_and_handles_product_database_failure():
    conn,cur=connection([{'id':1,'sources':'[]','relation_types':'[]','raw_json':'{}'}])
    cur.fetchone.return_value={'n':1}
    with patch.object(products,'target_connect',return_value=conn):
        packet=products.task_recommendations([5,6])
    assert packet['total']==1 and 'raw_json' not in packet['items'][0]
    assert all('SELECT' in c.args[0] and c.args[1]==[5,6] for c in cur.execute.call_args_list)
    conn.commit.assert_not_called()
    with patch.object(products,'target_connect',side_effect=AssertionError('should not connect')):
        assert products.task_recommendations([])['status']=='not_run'
    import pymysql
    with patch.object(products,'target_connect',side_effect=pymysql.OperationalError('private')):
        result=products.task_recommendations([5])
    assert result['status']=='unavailable' and 'private' not in result['message']
