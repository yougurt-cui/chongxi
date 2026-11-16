import json
from copy import deepcopy
from unittest.mock import MagicMock, patch
import pytest
from services import solution_knowledge_service as solution, model_knowledge_import_service as store
from services import multi_model_knowledge_service as multi, multi_model_prompt_service as prompts
from vendor.chongxi_multi_model_mysql import run


def document():
    return {'knowledge_base':'solution','question':'怎样记录换粮开始时间？','solutions':[{
        'title':'建立换粮时间记录','problem':'换粮后便臭，需要记录时间供回顾',
        'applicable_conditions':['猫咪正在更换主粮'],'goal':'区分首次喂新粮和完全换成新粮的日期',
        'steps':[{'action':'记录第一次喂新粮的日期；记不清时标记估计范围。','reason':'便于对照后续变化。'}],
        'record_fields':['首次喂新粮日期','完全换新粮日期','观察记录日期'],
        'verification':['对照记录核对变化先后，不据此直接认定因果。'],
        'stop_conditions':[],'source_urls':[],'evidence_status':'pending_verification'}]}


def db(existing=None, imported=False):
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    batch={'id':5,'knowledge_base':'solution','status':'success',
        'parsed_response':json.dumps(document()),'imported_at':'old' if imported else None}
    cur.fetchone.side_effect=lambda: {'acquired':1} if cur.execute.call_args.args[0].startswith('SELECT GET_LOCK') else batch
    cur.fetchall.return_value=existing or []
    return conn,cur


def test_solution_library_has_active_editable_templates_with_expected_schema():
    base=next(b for b in prompts.collection_bases()['bases'] if b['code']=='solution')
    assert base['label']=='解决方案知识库' and len(base['templates'])==2
    for template in base['templates']:
        assert '{{question}}' in template['template'] and '"solutions"' in template['template']
    payload={'knowledge_base':'solution','template_code':'solution_recording','providers':['qwen'],'question':'怎样记录换粮开始时间？'}
    with patch.object(run,'provider_configuration',return_value=[run.Provider('qwen','test','','test')]):
        assert multi.validate(payload)[3]=='solution'


def test_collector_parses_solution_schema_and_counts_deduplicated_solutions():
    doc=document();doc['solutions']*=2
    parsed=run.extract_json('```json\n'+json.dumps(doc,ensure_ascii=False)+'\n```')
    assert len(parsed['solutions'])==run.count_points(parsed)==1
    assert parsed['solutions'][0]['steps'][0]['reason']
    assert run.count_points({'solutions':[]})==0


@pytest.mark.parametrize('field,value',[('steps',[]),('steps',['do something']),('applicable_conditions',[]),
    ('record_fields','date'),('evidence_status','verified'),('source_urls',['javascript:alert(1)'])])
def test_malformed_solution_is_rejected(field,value):
    doc=document();doc['solutions'][0][field]=value
    with pytest.raises(ValueError):run.extract_json(json.dumps(doc))


def test_specialized_solution_routing_never_uses_selection_or_health_import():
    conn,cur=db()
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'_import_batch_source') as generic,patch.object(solution,'import_batch',return_value={'status':'completed'}) as specialized:
        assert store.import_batch(5)['status']=='completed'
    specialized.assert_called_once_with(5);generic.assert_not_called()


def test_import_preserves_structured_content_source_and_pending_review():
    conn,cur=db()
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn):
        result=solution.import_batch(5)
    assert result['inserted_count']==1
    inserts=[c for c in cur.execute.call_args_list if c.args[0].startswith('INSERT')]
    assert len(inserts)==1 and inserts[0].args[1][0]==5
    assert json.loads(inserts[0].args[1][-1])==document()['solutions'][0]
    assert all('product_recommendation' not in c.args[0] and 'health_knowledge' not in c.args[0] for c in cur.execute.call_args_list)
    conn.commit.assert_called_once()


def test_identical_import_preserves_review_and_changed_reviewed_import_rolls_back():
    import hashlib
    key=hashlib.sha256(store.dumps(document()['solutions'][0]).encode()).hexdigest()
    conn,cur=db([{'point_key':key,'review_status':'verified'}],True)
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn):
        assert solution.import_batch(5)['status']=='already_imported'
    assert not any(c.args[0].startswith('DELETE') for c in cur.execute.call_args_list)
    conn,cur=db([{'point_key':'old','review_status':'verified'}],True)
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn):
        with pytest.raises(ValueError,match='人工审核'):solution.import_batch(5)
    conn.rollback.assert_called_once();conn.commit.assert_not_called()


def test_solution_pipeline_does_not_normalize_selection_or_import_products():
    from services import product_recommendation_import_service as products
    conn,cur=db();cur.fetchone.side_effect=[{'acquired':1},{'knowledge_base':'solution'}]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'import_batch',return_value={'source_table':'solution_knowledge_point'}) as importer, \
         patch('services.knowledge_standardization_service.normalize') as mapping, \
         patch.object(products,'import_batch_recommendations') as product:
        assert store.process_batch_pipeline(5)['source_table']=='solution_knowledge_point'
    importer.assert_called_once_with(5);mapping.assert_not_called();product.assert_not_called()


def test_solution_task_detail_includes_specialized_result_and_model_sources():
    conn,cur=db()
    batch={'id':5,'task_id':1,'question':document()['question'],'prompt_template':'{{question}}',
           'web_search':False,'status':'success','knowledge_base':'solution','provider':'qwen','model':'test-model',
           'raw_response':json.dumps(document()),'parsed_response':json.dumps(document()),'search_metadata':None,
           'error_message':None,'import_error':None,'prompt_tokens':0,'completion_tokens':0}
    cur.fetchall.side_effect=[[batch],[]]
    with patch.object(multi,'ensure_tables'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'task_status',return_value={'batches':[]}), \
         patch.object(solution,'task_result',return_value={'points':[{'title':'建立记录'}]}) as result:
        detail=multi.task_detail(1)
    assert detail['task']['knowledge_base']=='solution' and detail['solution_structured']['points']
    assert detail['responses'][0]['model_name']=='test-model' and detail['assembled_points']==[]
    result.assert_called_once_with(1)


def test_task_list_filters_solution_and_uses_specialized_count():
    conn,cur=db();cur.fetchone.side_effect=None;cur.fetchone.return_value={'n':1}
    cur.fetchall.return_value=[{'id':1,'knowledge_base':'solution','point_count':2}]
    with patch.object(multi,'ensure_tables'),patch.object(store,'connect',return_value=conn):
        result=multi.list_tasks(knowledge_base='solution')
    assert result['items'][0]['point_count']==2
    sql,params=cur.execute.call_args.args
    assert 'solution_knowledge_point' in sql and params==['solution',0]
