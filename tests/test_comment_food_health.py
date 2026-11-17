import json
from copy import deepcopy
from unittest.mock import MagicMock
import pytest
from comment_pipeline.common.cleaner import normalize_text
from comment_pipeline.llm_router import validate_route,validate_events,fingerprint
from comment_pipeline.food_health import eligible,process,save_result


def row():
    return {'comment_id':21,'platform':'douyin','source_id':'x','raw_text':'@小明 [流泪] 我家猫吃皇家后软便，换成卫仕后好了。',
            'clean_text':'我家猫吃皇家后软便，换成卫仕后好了。','source_title':'猫粮选择','source_content':'','source_keyword':'猫粮'}


def route():
    return {'domains':['pet_food'],'processors':['product_feedback','health_observation','solution_experience'],
            'domain_evidence':{'pet_food':'猫粮'},'processor_evidence':{'product_feedback':'吃皇家后软便','health_observation':'软便','solution_experience':'换成卫仕后好了'},
            'species':'cat','species_evidence':'猫','species_evidence_source':'comment'}


def event():
    return {'brand_name':'卫仕','product_name':'','symptom_category':'消化系统问题','symptom_name':'软便/拉稀',
            'observation':'improved','effect_direction':'改善','evidence_text':'换成卫仕后好了',
            'confidence':.9,'time_text':'','action_text':'换成卫仕'}


def test_cleaning_preserves_negation_and_removes_emotional_emoji():
    assert normalize_text('@小明 [流泪] 我家猫从没软便😭')=='我家猫从没软便'


def test_context_domain_evidence_is_distinguished_from_comment_evidence():
    result=validate_route(route(),row())
    assert result['evidence_sources']['pet_food']=='source_title'
    assert result['evidence_sources']['health_observation']=='comment'
    assert eligible(result)


@pytest.mark.parametrize('change',[
    {'processors':['health_observation'],'processor_evidence':{'health_observation':'猫粮'}},
    {'domains':['food'],'domain_evidence':{'food':'猫粮'}},
    {'domains':['pet_food','pet_food']},
    {'species':'dog'},
])
def test_invalid_tags_or_context_only_health_evidence_rejected(change):
    value=route();value.update(change)
    with pytest.raises(ValueError):validate_route(value,row())


def test_no_brand_required_and_dog_or_unknown_not_sent_to_cat_tagger():
    value=validate_route(route(),row());value['processors']=['health_observation']
    assert eligible(value)
    for species in ('dog','unknown'):
        value['species']=species;assert not eligible(value)


def test_events_preserve_brand_relationship_and_require_exact_evidence():
    assert validate_events({'events':[event(),event()]},row())==[event()]
    for field,value in [('brand_name','冠能'),('evidence_text','换粮后改善'),('symptom_category','眼部问题'),('confidence',2),('time_text','昨天')]:
        item=event();item[field]=value
        with pytest.raises(ValueError):validate_events({'events':[item]},row())


def test_absence_does_not_become_improvement_or_worsening():
    item=event();item.update(observation='absent',effect_direction='加重')
    with pytest.raises(ValueError):validate_events({'events':[item]},row())
    item['effect_direction']='不确定'
    assert validate_events({'events':[item]},row())[0]['observation']=='absent'


def test_fingerprint_changes_with_context_model_or_text():
    original=row();other=deepcopy(original);other['source_keyword']='狗粮'
    assert fingerprint(original,'qwen')!=fingerprint(other,'qwen')
    assert fingerprint(original,'qwen')!=fingerprint(original,'other')


def test_classifier_failure_never_calls_tagger_or_looks_like_empty_success():
    classifier=MagicMock();classifier.classify.return_value={'status':'failed','error':'timeout'}
    assert process(row(),classifier)['status']=='route_failed'
    classifier.tag.assert_not_called()


def test_general_food_comment_is_classified_without_symptom_extraction():
    classifier=MagicMock();r=validate_route(route(),row());r['processors']=['product_feedback']
    classifier.classify.return_value={'status':'success','result':r}
    assert process(row(),classifier)['status']=='classified'
    classifier.tag.assert_not_called()


def test_persisted_candidates_are_pending_and_duplicate_updates_do_not_overwrite_review():
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value;cur.lastrowid=20;cur.rowcount=1;cur.fetchone.return_value=None
    result={'status':'tagged','classification':{'status':'success','result':validate_route(route(),row())},
            'health_annotation':{'status':'success','result':[event()]}}
    save_result(conn,row(),result,'trial','qwen')
    inserts=[c.args for c in cur.execute.call_args_list if 'INSERT INTO cat_disease_clue_candidates' in c.args[0]]
    assert len(inserts)==1 and "'PENDING'" in inserts[0][0]
    assert inserts[0][0].split('ON DUPLICATE KEY UPDATE')[1].strip()=='id=LAST_INSERT_ID(id)'
    assert 'cat_disease_clues' not in '\n'.join(c.args[0] for c in cur.execute.call_args_list)


def test_absent_event_is_audited_but_not_inserted_as_disease_candidate():
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value;cur.lastrowid=20
    item=event();item.update(observation='absent',effect_direction='不确定')
    result={'status':'tagged','classification':{'status':'success','result':validate_route(route(),row())},
            'health_annotation':{'status':'success','result':[item]}}
    save_result(conn,row(),result,'trial','qwen')
    assert not any('INSERT INTO cat_disease_clue_candidates' in c.args[0] for c in cur.execute.call_args_list)


def test_validation_retry_includes_specific_feedback_and_recovers():
    from comment_pipeline.llm_router import Classifier,ROUTER_PROMPT
    from types import SimpleNamespace
    classifier=Classifier.__new__(Classifier)
    classifier.config={'model':'test','api_key':'test-secret'};classifier.retries=1;classifier.client=MagicMock()
    invalid=route();invalid['species_evidence']='我家狗'
    def response(value):return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))],usage=None)
    classifier.client.chat.completions.create.side_effect=[response(invalid),response(route())]
    result=classifier.request(ROUTER_PROMPT,row(),validate_route)
    assert result['status']=='success' and result['attempts']==2
    messages=classifier.client.chat.completions.create.call_args.kwargs['messages']
    assert '物种证据无效' in messages[-1]['content']


def test_food_flow_dry_run_does_not_connect_or_call_models():
    from argparse import Namespace
    from unittest.mock import patch
    from comment_pipeline.run_pipeline import run
    args=Namespace(flow='food-health',dry_run=True,limit=100)
    with patch('comment_pipeline.food_health.connect') as connect:
        result=run(args)
    assert result['llm_calls']==0 and result['writes'] is False
    connect.assert_not_called()


def test_hairball_in_stool_does_not_support_inferred_constipation():
    r=row();r['clean_text']='我这段时间坚持每天喂化毛冻干，后面便便里面有毛球排出来'
    item=event();item.update(brand_name='',action_text='喂化毛冻干',symptom_name='便秘/干硬便',evidence_text='便便里面有毛球排出来')
    with pytest.raises(ValueError,match='不得从其他现象推断'):
        validate_events({'events':[item]},r)


def test_existing_candidate_is_linked_without_update_or_incorrect_insert_count():
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    cur.lastrowid=20;cur.fetchone.return_value={'id':17}
    result={'status':'tagged','classification':{'status':'success','result':validate_route(route(),row())},
            'health_annotation':{'status':'success','result':[event()]}}
    save_result(conn,row(),result,'trial','qwen')
    assert result['candidate_ids']==[17] and result['new_candidates']==0
    assert not any('INSERT INTO cat_disease_clue_candidates' in c.args[0] for c in cur.execute.call_args_list)
