import hashlib
import json
from unittest.mock import MagicMock,patch
import pytest
from services import behavior_model_pipeline as pipeline, model_knowledge_import_service as store
from vendor.chongxi_multi_model_mysql import run
from tests.test_behavior_knowledge import document


def connection():
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value;cur.lastrowid=7
    return conn,cur


def test_behavior_collection_records_successful_api_text_without_parsing_it():
    provider=run.Provider('qwen','test-key','https://example.com','test-model')
    with patch('services.model_native_search_service.call',return_value=('not JSON',{},{})), \
         patch.object(run,'extract_json') as parse:
        result=run.call_provider(provider,'request',web_search=True,knowledge_base='behavior')
    assert result['status']=='success' and result['raw_text']=='not JSON' and result['parsed'] is None
    assert result['request_prompt']=='request'
    parse.assert_not_called()


def test_behavior_auto_parse_runs_only_after_raw_call_commit():
    conn,cur=connection();cur.fetchone.return_value={'id':5,'knowledge_base':'behavior'}
    result={'provider':'qwen','status':'success','raw_text':'not JSON','parsed':None,
            'search_metadata':{},'error':None,'latency_ms':1,'request_prompt':'actual request'}
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(pipeline,'snapshot_call') as snapshot,patch.object(store,'process_batch_pipeline') as parse, \
         patch.object(pipeline,'parse_batch',side_effect=lambda batch_id: conn.commit.assert_called_once()) as behavior_parse:
        store.persist_model_result(1,result)
    snapshot.assert_called_once_with(cur,5,request_prompt='actual request');parse.assert_not_called()
    behavior_parse.assert_called_once_with(5)
    assert all('behavior_knowledge_point_detail' not in c.args[0] for c in cur.execute.call_args_list)


def test_call_uses_original_batch_without_dedicated_call_table():
    conn,cur=connection();cur.fetchone.return_value={'id':5}
    assert pipeline.snapshot_call(cur,5,request_prompt='rendered')==5
    assert all('behavior_model_call' not in c.args[0] for c in cur.execute.call_args_list)
    assert 'behavior_parse_result' in cur.execute.call_args_list[-1].args[0]


def test_parse_saves_state_then_calls_summary():
    conn,cur=connection()
    batch={'id':5,'status':'success','raw_response':json.dumps(document()),'question':document()['question']}
    cur.fetchone.side_effect=[{'acquired':1},batch]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch('services.behavior_knowledge_service._import_points',return_value={'total_count':1,'status':'completed'}) as writer, \
         patch('services.behavior_knowledge_summary_service.refresh',return_value={'total':1}) as summarize:
        result=pipeline.parse_batch(5)
    writer.assert_called_once();summarize.assert_called_once()
    assert result['summary']=={'total':1}
    assert all('behavior_model_call' not in c.args[0] for c in cur.execute.call_args_list)


def test_parse_failure_is_logged_separately_and_does_not_change_successful_call():
    conn,cur=connection();cur.fetchone.side_effect=[{'acquired':1},{'status':'success','raw_response':'not JSON','question':'q'}]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(pipeline,'ensure_call',return_value=7),patch.object(run,'call_provider') as model:
        with pytest.raises(ValueError):pipeline.parse_batch(5)
    model.assert_not_called()
    statements=[c.args[0] for c in cur.execute.call_args_list]
    assert any('behavior_parse_result=%s' in c.args[0] and json.loads(c.args[1][0])['parse_status']=='failed' for c in cur.execute.call_args_list)
    assert not any('SET status=' in s or 'SET raw_response=' in s for s in statements)


def test_manual_task_import_does_not_rewrite_raw_call_status_or_reinvoke_model():
    conn,cur=connection();cur.fetchall.return_value=[{'id':5,'knowledge_base':'behavior','status':'parse_failed'}]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(pipeline,'parse_batch',return_value={'status':'completed'}) as parse, \
         patch.object(store,'_reparse_batch') as legacy,patch.object(run,'call_provider') as model:
        result=store.import_task(1)
    assert result['status']=='completed'
    parse.assert_called_once_with(5);legacy.assert_not_called();model.assert_not_called()


@pytest.mark.parametrize('status,raises,expected',[('api_failed',False,0),('success',True,1)])
def test_auto_parse_failure_preserves_call_and_failed_calls_are_skipped(status,raises,expected):
    conn,cur=connection();cur.fetchone.return_value={'id':5,'knowledge_base':'behavior'}
    result={'provider':'qwen','status':status,'raw_text':'not JSON','parsed':None,
            'error':None,'latency_ms':1}
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(pipeline,'snapshot_call'),patch.object(pipeline,'parse_batch',side_effect=ValueError('bad JSON') if raises else None) as parse, \
         patch.object(store.LOGGER,'exception') as log:
        store.persist_model_result(1,result)
    assert parse.call_count==expected
    conn.commit.assert_called_once();conn.rollback.assert_not_called()
    assert log.call_count==int(raises)
