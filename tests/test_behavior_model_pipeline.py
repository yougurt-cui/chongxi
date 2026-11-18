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


def test_persisting_behavior_call_never_automatically_parses_or_inserts_knowledge():
    conn,cur=connection();cur.fetchone.return_value={'id':5,'knowledge_base':'behavior'}
    result={'provider':'qwen','status':'success','raw_text':'not JSON','parsed':None,
            'search_metadata':{},'error':None,'latency_ms':1,'request_prompt':'actual request'}
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(pipeline,'snapshot_call') as snapshot,patch.object(store,'process_batch_pipeline') as parse:
        store.persist_model_result(1,result)
    snapshot.assert_called_once_with(cur,5,request_prompt='actual request');parse.assert_not_called()
    assert all('behavior_knowledge_point_detail' not in c.args[0] for c in cur.execute.call_args_list)


def test_raw_snapshot_is_idempotent_and_rejects_overwriting_history():
    conn,cur=connection()
    batch={'id':5,'knowledge_base':'behavior','status':'parse_failed','raw_response':'raw','prompt_template':'template'}
    old={'id':7,'response_hash':hashlib.sha256(b'raw').hexdigest(),'prompt_template':'template'}
    cur.fetchone.side_effect=[batch,old]
    assert pipeline.snapshot_call(cur,5)==7
    assert not any(c.args[0].startswith(('INSERT','UPDATE','DELETE')) for c in cur.execute.call_args_list)
    cur.fetchone.side_effect=[{**batch,'raw_response':'changed'},old]
    with pytest.raises(ValueError,match='不覆盖'):pipeline.snapshot_call(cur,5)


def test_explicit_parse_reads_saved_response_and_logs_success_without_calling_models():
    conn,cur=connection();call={'id':7,'call_status':'success','raw_response':json.dumps(document()),
        'question':document()['question'],'response_hash':'a'*64}
    cur.fetchone.side_effect=[{'acquired':1},call]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(pipeline,'ensure_call',return_value=7), \
         patch('services.behavior_knowledge_service._import_points',return_value={'total_count':1,'status':'completed'}) as writer, \
         patch.object(run,'call_provider') as model:
        result=pipeline.parse_batch(5)
    assert result['source_call_id']==7
    writer.assert_called_once_with(5,document(),call_id=7,source_hash='a'*64,parser_version=pipeline.PARSER_VERSION)
    model.assert_not_called()
    statements=[c.args[0] for c in cur.execute.call_args_list]
    assert any(c.args[0].startswith('UPDATE behavior_model_call SET parse_status=%s') and c.args[1][0]=='completed' for c in cur.execute.call_args_list)
    assert not any(s.startswith('UPDATE knowledge_import_batch') or 'SET call_status=' in s or 'SET raw_response=' in s for s in statements)


def test_parse_failure_is_logged_separately_and_does_not_change_successful_call():
    conn,cur=connection();cur.fetchone.side_effect=[{'acquired':1},{'call_status':'success','raw_response':'not JSON','question':'q','response_hash':'a'*64}]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(pipeline,'ensure_call',return_value=7),patch.object(run,'call_provider') as model:
        with pytest.raises(ValueError):pipeline.parse_batch(5)
    model.assert_not_called()
    statements=[c.args[0] for c in cur.execute.call_args_list]
    assert any(c.args[0].startswith('UPDATE behavior_model_call SET parse_status=%s') and c.args[1][0]=='failed' for c in cur.execute.call_args_list)
    assert not any(s.startswith('UPDATE knowledge_import_batch') or 'SET call_status=' in s or 'SET raw_response=' in s for s in statements)


def test_manual_task_import_does_not_rewrite_raw_call_status_or_reinvoke_model():
    conn,cur=connection();cur.fetchall.return_value=[{'id':5,'knowledge_base':'behavior','status':'parse_failed'}]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(pipeline,'parse_batch',return_value={'status':'completed'}) as parse, \
         patch.object(store,'_reparse_batch') as legacy,patch.object(run,'call_provider') as model:
        result=store.import_task(1)
    assert result['status']=='completed'
    parse.assert_called_once_with(5);legacy.assert_not_called();model.assert_not_called()
