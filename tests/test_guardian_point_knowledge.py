"""Requirement 49: new schema, provenance, applicability and all consultation routes."""
import json
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pymysql
import pytest
from services import guardian_point_knowledge as points
from services import guardian_knowledge_service as qa, guardian_knowledge_cards as cards
from services import miniprogram_chat_service as chat

DICTS = {
 'dict_breed': [dict(species_code='cat',breed_code='ragdoll',breed_name='布偶猫',aliases_pipe='布偶|Ragdoll',status='启用'),
                dict(species_code='cat',breed_code='british_shorthair',breed_name='英国短毛猫',aliases_pipe='英短',status='启用')],
 'dict_life_stage': [dict(species_code='cat',stage_code=code,status='启用',age_min_months_inclusive=lo,age_max_months_exclusive=hi)
                    for code,lo,hi in [('adult',12,84),('kitten',0,12),('senior',84,None),('all_stages',None,None),('unspecified',None,None)]],
 'dict_user_need': [dict(need_code='soft_stool',need_name='软便',aliases_pipe='便便软')],
 'dict_knowledge_topic': [dict(category_code='feeding_nutrition',topic_code='protein_nutrition',topic_name='蛋白质',aliases_pipe='蛋白'),
                          dict(category_code='behavior_daily',topic_code='scratching',topic_name='抓挠',aliases_pipe='沙发')],
}
PET = dict(animal_type='cat',breed='布偶',age_months=24)

def point(**changes):
    row = dict(id=1,batch_id=2,source_provider='deepseek',source_model='deepseek-v4-pro',task_id=1,
               species_code='cat',breed_code='ragdoll',life_stage_codes='["adult"]',need_codes='[]',
               original_condition='布偶成猫',category_code='feeding_nutrition',topic_code='protein_nutrition',
               content='动物蛋白提供猫所需的氨基酸，需结合完整配方判断。',relevance=5,
               standardization_meta='{"provider":"qwen","status":"success"}')
    row.update(changes)
    return row

def connection(rows):
    conn = MagicMock(); cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [deepcopy(DICTS[k]) for k in points.DICTIONARIES] + [rows]
    return conn, cur

def test_source_batch_provenance_parameterized_sql_no_legacy_and_balanced_providers():
    rows = [point(id=i,content=f'蛋白质相关资料示例{i}。') for i in range(1,16)] + [point(id=20,source_provider='qwen'),point(id=21,source_provider='doubao')]
    conn,cur=connection(rows)
    with patch.object(points,'_connect',return_value=conn):
        result=qa.retrieve(qa.plan_query("蛋白' OR 1=1 --",PET))
    sql,params=cur.execute.call_args.args
    assert 'knowledge_point_source' in sql and 'knowledge_import_batch' in sql
    assert "b.status='success'" in sql and 'k.source_provider=b.provider' in sql
    assert 'kd_problem_guides' not in sql and 'knowledge_base' not in sql
    assert 'OR 1=1 --' not in sql and sql.index('ORDER BY')<sql.index('LIMIT')
    assert len(result['items'])==12
    assert {r['source_provider'] for r in result['items']}=={'deepseek','qwen','doubao'}
    assert all(r['evidence_status']=='待验证' for r in result['items'])

def test_stage_unknown_is_not_all_stages_and_original_constraints_survive_mapping():
    pet={**PET,'breed_code':'ragdoll'};stages=DICTS['dict_life_stage']
    assert points.applicable(point(),pet,stages)
    for age in (None,5,90):
        assert not points.applicable(point(life_stage_codes='["unspecified"]'),{**pet,'age_months':age},stages)
    assert points.applicable(point(life_stage_codes='["all_stages"]',original_condition='所有布偶猫'),{**pet,'age_months':None},stages)
    assert not points.applicable(point(original_condition='布偶猫；≥8岁老年猫',life_stage_codes='["senior"]'),{**pet,'age_months':90},stages)
    assert not points.applicable(point(original_condition='确诊HCM的布偶猫'),pet,stages)
    assert not points.applicable(point(),{**pet,'breed_code':'british_shorthair'},stages)
    assert not points.applicable(point(),{**pet,'animal_type':'dog'},stages)
    assert not points.applicable(point(life_stage_codes='["invented"]'),pet,stages)

def test_explicit_breed_and_age_override_profile_without_borrowing_other_pets_age():
    scope=points.scope_for(qa.plan_query('英短蛋白怎么选',PET),DICTS)
    assert scope['breed_code']=='british_shorthair' and 'age_months' not in scope
    scope=points.scope_for(qa.plan_query('布偶幼猫蛋白怎么选',PET),DICTS)
    assert scope['explicit_stage']=='kitten' and 'age_months' not in scope
    scope=points.scope_for(qa.plan_query('6个月布偶猫的蛋白',{}),DICTS)
    assert scope['breed_code']=='ragdoll' and scope['age_months']==6
    scope=points.scope_for(qa.plan_query('不是布偶，是英短的蛋白',PET),DICTS)
    assert scope['breed_code']=='british_shorthair'

def test_database_outage_does_not_fall_back_and_empty_query_does_not_connect():
    with patch.object(points,'_connect',side_effect=pymysql.OperationalError('offline')) as connect:
        assert qa.retrieve(qa.plan_query('蛋白',PET))['status']=='unavailable'
        connect.reset_mock()
        assert qa.retrieve(qa.plan_query('你好',PET))['status']=='no_match'
        connect.assert_not_called()

def test_failure_never_exposes_unsynthesized_model_claims():
    row=point(content='营养至少百分之四十',source_table='knowledge_point_source',status='待审核',evidence_status='待验证')
    with patch.object(qa,'get_chat_model_config',return_value={'api_key':''}):
        reply,mode,ids,data,note=qa.structured_answer('问题',{}, {}, {'status':'matched','items':[row]})
    assert mode=='synthesis_unavailable' and not ids and '百分之四十' not in reply
    assert all(c['type']!='community' for c in data)


def test_duplicate_branches_keep_provenance_without_filling_candidate_budget():
    conn, _ = connection([point(id=1), point(id=2), point(id=3, source_provider='qwen')])
    with patch.object(points, '_connect', return_value=conn):
        items = qa.retrieve(qa.plan_query('蛋白质', PET))['items']
    assert [r['id'] for r in items] == [1, 3]
    assert items[0]['duplicate_point_ids'] == [2]

def test_conflicts_require_two_real_citations_and_cannot_output_unverified_thresholds():
    rows=[point(id=1),point(id=2)]
    data=[cards.card('conclusion','现有资料有分歧。',[1]),cards.card('cautions','不能据此确定统一比例。',[1,2])]
    raw={'conflicts':[{'source_ids':[1,2],'summary':'两种比例不同'}]}
    cards.validate_points(data,raw,rows)
    qualified = deepcopy(data)
    qualified[1]['content'] = '不同资料建议30%或40%，存在分歧，不能据此确定统一标准。'
    cards.validate_points(qualified, raw, rows)
    with pytest.raises(ValueError):
        cards.validate_points(qualified, {'conflicts': []}, rows)
    for bad in ({'conflicts':[{'source_ids':[1,999],'summary':'矛盾'}]}, {'conflicts':None}):
        with pytest.raises(ValueError):cards.validate_points(data,bad,rows)
    with pytest.raises(ValueError):cards.validate_points([cards.card('conclusion','必须至少40%。',[1])],{'conflicts':[]},rows)
    with pytest.raises(ValueError):cards.validate_points([cards.card('community','社区经验',[1])],{'conflicts':[]},rows)

def test_photo_health_uses_new_knowledge_and_urgent_bypasses_it():
    conv={'id':'c','pet_id':'p'};scene={'task':'health_consult','reply':'unverified photo model text'}
    with patch.object(qa,'handle_question',return_value={'reply':'grounded','result':{}}) as call:
        reply=chat._scene_reply('u',conv,'它掉毛怎么办',chat._default_state('c'),scene,[{'visible':'毛发稀疏'}],None,'m')
        assert reply['reply']=='grounded' and call.call_args.kwargs['consultation']=='symptom_consult'
        assert '毛发稀疏' in call.call_args.args[2]
        call.reset_mock()
        reply=chat._scene_reply('u',conv,'呼吸困难',chat._default_state('c'),scene,[],None,'m')
        assert reply['response_type']=='risk_alert';call.assert_not_called()

def test_soft_stool_supplement_preserves_state_turn_and_original_actions():
    result={'state':{'turn':1,'slots':{},'slot_status':{},'analysis':{}},'status':'answered','reply':'原有分析',
            'reply_parts':[{'response_type':'text','reply':'原有分析'}],'actions':[{'label':'继续观察'}]}
    supplement={'reply':'整合知识','result':{'pipeline':'knowledge_qa','data':[cards.card('conclusion','整合知识',[1])],
        'knowledge_qa':{'generation_source':'model_structured_answer','point_ids':[1]}}}
    state=chat._default_state('c')
    with patch.object(chat,'handle_soft_stool_turn',return_value=result),patch.object(qa,'handle_question',return_value=supplement):
        reply=chat._handle_soft_stool_message('u',{'id':'c','pet_id':'p'},'情况都说明了',state,'m')
    assert state['turn_count']==1 and state['primary_intent']=='symptom_consult'
    assert state['slots'][chat.SOFT_STOOL_STATE_SLOT]['status']=='answered'
    assert reply['messages'][0]['reply']=='原有分析'
    assert reply['messages'][-1]['result']['pipeline']=='knowledge_qa'
    assert reply['messages'][-1]['result']['turn_no']==1

def test_report_and_factor_evidence_pause_legacy_queries():
    from services import guardian_report_service as report,guardian_factor_evidence_service as factors
    assert factors.load_community_evidence()==[]
    conn=MagicMock();cur=conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[]
    with patch.object(report,'_connect',return_value=conn),patch.object(qa,'retrieve') as retrieve:
        result=report.community_evidence({'animal_type':'cat'},[{'brand':'测试牌','cautions':[]}])
    retrieve.assert_not_called()
    assert result['source_status']['kd_problem_guides']=='paused'
