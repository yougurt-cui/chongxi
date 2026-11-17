import json
from copy import deepcopy
from unittest.mock import MagicMock, patch
import pytest
from services import guardian_health_solution as sol
from services import guardian_product_recommendation as rec
from services import guardian_conversation_context as ctx
from services import guardian_task_knowledge as health

PLAN = {'query': '成年猫便臭，精神食欲正常', 'species': 'cat', 'pet_scope': {'animal_type': 'cat', 'age_months': 24}}

def row(**kw):
    return dict(id=7,species='cat',breed='all',life_stage='all',scenario='近期换粮，精神食欲正常且便便成形',
                possible_causes=['食物消化不良'],solution=['记录每日进食量和粪便形态，避免过度喂食', '补充益生菌，每次2g'],
                verification={'expected_result':'粪便形态保持成型','review_condition':'禁食后8小时'},
                review_status='pending_review',sources=[{'source_key':'batch:7','source_model':'test'}]) | kw

def groups():
    return [dict(cause='消化不良', mechanism='残渣发酵',manifestations=[],supporting=[],source_ids=[101])]

def test_extract_preserves_cause_and_single_source_without_treatment_protocol():
    g=groups()
    with patch.object(sol,'load_candidates',return_value=[row()]), patch.object(sol,'_model',return_value={'matches':[dict(group_index=0,solution_id=7,cause_index=0)]}):
        sol.enrich(PLAN,g,[dict(id=101,cause='消化不良')])
    s=g[0]['solution'];assert s['source_id']==7 and s['linked_cause']=='食物消化不良'
    assert s['steps']==['记录每日进食量和粪便形态，避免过度喂食']
    assert '禁食' not in sol.plain_text(s) and '2g' not in sol.plain_text(s)
    assert s['scenario']==row()['scenario'] and s['sources'][0]['source_key']=='batch:7'

@pytest.mark.parametrize('bad',[{'group_index':0,'solution_id':999,'cause_index':0},
    {'group_index':True,'solution_id':7,'cause_index':0},
    {'group_index':0,'solution_id':7,'cause_index':-1},
    {'group_index':0,'solution_id':7,'cause_index':3},'not-a-match'])
def test_model_cannot_invent_sources_or_cause_indexes(bad):
    g=groups()
    with patch.object(sol,'load_candidates',return_value=[row()]),patch.object(sol,'_model',return_value={'matches':[bad]}):
        sol.enrich(PLAN,g,[])
    assert g[0]['solution']['status']=='unavailable' and g[0]['cause']=='消化不良'

def test_solution_failure_does_not_hide_health_causes():
    with patch.object(health,'identify_issues',return_value=[dict(issue_code='STOOL_ODOR',issue_name='便臭',high_risk_warning=False)]), patch.object(health,'health_rows',return_value=[dict(id=101,batch_id=1,source_provider='x',source_model='x',review_status='pending')]), patch.object(health,'summarize_health',return_value=groups()),patch.object(sol,'load_candidates',side_effect=TimeoutError):
        r=health.health_answer(PLAN)
    assert r['status']=='matched' and r['groups'][0]['solution']['status']=='unavailable'

def test_empty_solution_and_urgent_do_not_call_model():
    with patch.object(sol,'load_candidates',return_value=[]),patch.object(sol,'_model') as model:
        g=groups();sol.enrich(PLAN,g,[]);assert g[0]['solution']['status']=='no_match';model.assert_not_called()
    with patch.object(health,'identify_issues',return_value=[dict(issue_code='BLOOD',high_risk_warning=True)]),patch.object(sol,'enrich') as enrich:
        assert health.health_answer(PLAN)['status']=='risk_alert';enrich.assert_not_called()

@pytest.mark.parametrize('text',['立即禁食8小时','每日补充益生菌','观察后每天给2mg药物','采用7日换粮法，每天增加10%新粮','记录后立即改用水煮鸡肉','<script>观察</script>'])
def test_unsafe_source_steps_not_shown(text):
    assert not sol.safe_step(text)

def test_scope_filters_species_breed_numeric_age_and_unknown_age():
    stages=[dict(species_code='cat',stage_code=s,status='启用',age_min_months_inclusive=a,age_max_months_exclusive=b) for s,a,b in [('adult',12,84),('kitten',0,12),('senior',84,None),('all_stages',None,None)]]
    records=[row(),row(id=8,species='dog'),row(id=9,breed='ragdoll'),row(id=10,life_stage='kitten'),row(id=11,scenario='老年猫（≥10岁）便臭')]
    conn=MagicMock();cur=conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect=[[],stages,deepcopy(records)]
    with patch.object(sol,'_connect',return_value=conn):
        assert [r['id'] for r in sol.load_candidates(PLAN)]==[7]
    assert all(c.args[0].lstrip().startswith('SELECT') for c in cur.execute.call_args_list)
    cur.fetchall.side_effect=[[],stages,deepcopy(records)]
    with patch.object(sol,'_connect',return_value=conn):
        assert [r['id'] for r in sol.load_candidates(dict(PLAN,pet_scope={'animal_type':'cat'}))]==[7]

def packet():
    items=[dict(product_key='key1',brand='原品牌',product_name='原产品',entity_level='product',function_summary='原始功能描述',cautions=['核对具体版本'])]
    return dict(version=1,status='ready',pet_id='pet',items=items,**rec.followup_questions(items))

def test_guidance_topics_adapt_to_product_count_without_fake_formula_ids():
    p=packet();assert len(p['questions'])==3 and set(p['question_topics'].values())=={'compare','nutrition','fit'}
    assert '这款' in p['questions'][0]
    assert '这几款' in rec.followup_questions(p['items']*2)['questions'][0]
    assert '品牌' in rec.followup_questions([dict(entity_level='brand')])['questions'][0]
    assert not rec.followup_questions([])['questions']

def test_saved_card_scope_and_question_validation():
    p=packet();source=dict(id='old-card',role='assistant',content='推荐',result_json=json.dumps({'product_recommendations':p}))
    conn=MagicMock();cur=conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[];cur.fetchone.return_value=source
    conv=dict(id='conv',pet_id='pet');state={'slots':{},'turn_count':0}
    memory=ctx.load(lambda:conn,'owner',conv,state,'old-card',p['questions'][0])
    assert memory['recommendation_source']['items']==p['items']
    assert not memory['products'] # recommendation keys never become scored formula IDs
    for question in p['questions']:
        answer=rec.followup(question,memory,state)
        assert answer['result']['recommendation_followup']['source_message_id']=='old-card'
        assert answer['result']['recommendation_followup']['product_keys']==['key1']
        assert '原品牌' in answer['reply'] and '原产品' in answer['reply']
    for c,q in [(conv,'伪造问题'),(dict(conv,pet_id='other'),p['questions'][0])]:
        with pytest.raises(ValueError):ctx.load(lambda:conn,'owner',c,state,'old-card',q)
    cur.fetchone.return_value=None
    with pytest.raises(ValueError):ctx.load(lambda:conn,'other-user',conv,state,'old-card',p['questions'][0])
    clean=ctx.load(lambda:conn,'owner',conv,{'slots':{ctx.SLOT:memory}})
    assert 'recommendation_source' not in clean
