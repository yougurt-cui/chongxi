import json
from unittest.mock import MagicMock, patch
import pytest
from services import guardian_task_knowledge as task, guardian_knowledge_service as qa, guardian_health_flow_service as flow


def dim(key,a,b,maximum=1,kind='higher_is_support'):
 return dict(key=key,title=key,delta=b-a,status='comparable',left=dict(score=a,scale_max=maximum,direction=kind),right=dict(score=b,scale_max=maximum,direction=kind))
def pair(ds):return dict(left_id='a',right_id='b',left_label='A',right_label='B',dimensions=ds,status='comparable',note='参考')
def rule(i,metric,key,direction,good):
 return dict(id=i,metric_code=metric,source_field=f'$.portrait.dimensions.{key}.score',canonical_factor_code=metric.upper(),change_direction=direction,recommendation_text=f'解释{i}',owner_observation=f'观察{i}',indicator_type='beneficial' if good else 'harmful')
def connection(rows):
 c=MagicMock();q=c.__enter__.return_value.cursor.return_value.__enter__.return_value;q.fetchall.return_value=rows
 return c,q
ISSUE=dict(issue_code='STOOL_ODOR',issue_name='便臭',aliases='猫屎臭',high_risk_warning=0)
GROUP=dict(cause='消化不充分',mechanism='未消化部分进入后肠。',manifestations=['气味变化'],supporting=['近期是否换粮？'],source_ids=[1,2])


def summary_points(points):
 return [dict(id=p['id'],issue_code='SOFT_STOOL',causal_chains=[dict(cause='高脂饮食',nutrition_factors=p['nutrition_factors'],manifestations=p['manifestations'])]) for p in points]


def test_normalized_top3_signed_advantages_and_exact_health_links():
 ds=[dim('protein_quality',.3,.9),dim('fat',.2,.7,kind='higher_is_burden'),dim('fiber',1,3,5),dim('prebiotic',1,2,5)]
 rules=[rule(1,'protein_quality','protein_quality','increase',True),rule(2,'protein_quality','protein_quality','decrease',False),rule(3,'fat_pressure','fat','increase',False),rule(4,'fat_pressure','fat','decrease',True)]
 points=[dict(id=99,manifestations=['偏软'],nutrition_factors=[dict(factor='FAT_BURDEN',direction='increase',relation='受耐受影响')]),dict(id=100,manifestations=['错误关联'],nutrition_factors=[dict(factor='crude_protein',direction='increase',relation='含量不是评分')])]
 with patch.object(task,'load_rules',return_value=rules),patch.object(task,'switch_health_rows',return_value=summary_points(points)):
  result=task.nutrition_pairs([pair(ds)],'cat')['pairs'][0]
  reverse=task.nutrition_pairs([pair([{**d,'delta':-d['delta'],'left':d['right'],'right':d['left']} for d in ds])],'cat')['pairs'][0]
 assert [d['key'] for d in result['top_changes']]==['protein_quality','fat','fiber']
 assert not result['products'][0]['strengths']
 assert not result['products'][1]['strengths']
 assert [{k:l[k] for k in ('source_id','relation','manifestations')} for l in result['health_links']]==[dict(source_id=99,relation='',manifestations=['偏软'])]
 assert not reverse['products'][0]['strengths'] and not reverse['health_links']


def test_missing_mapping_equal_or_incomparable_never_invent():
 ds=[dim('protein',.2,.5,kind='higher_is_burden'),dim('fiber',1,1,5)]
 points=[dict(id=1,manifestations=['便臭'],nutrition_factors=[dict(factor='crude_protein',direction='increase',relation='原始营养量')])]
 with patch.object(task,'load_rules',return_value=[rule(1,'protein_pressure','protein','increase',False)]),patch.object(task,'switch_health_rows',return_value=summary_points(points)):
  r=task.nutrition_pairs([pair(ds)],'cat')['pairs'][0]
  assert len(r['top_changes'])==1 and not r['health_links'] and r['health_note']
  assert not r['products'][0]['strengths'] and not r['products'][1]['strengths']
  ds[0].update(status='not_comparable',delta=None)
  assert not task.nutrition_pairs([pair(ds)],'cat')['pairs'][0]['top_changes']


def test_sql_read_only_species_and_provider_constraints_and_parameters():
 c,q=connection([dict(manifestations='[]',verification='{}',nutrition_factors='[]')])
 with patch.object(task,'_connect',return_value=c):
  assert task.health_rows(None)==[];q.execute.assert_not_called()
  task.health_rows('cat',["x' OR 1=1 --"])
 sql,params=q.execute.call_args.args
 assert params==['cat',"x' OR 1=1 --"] and 'OR 1=1' not in sql
 assert "b.status='success'" in sql and 'b.provider=h.source_provider' in sql and 'h.species_code=%s' in sql
 c,q=connection([])
 with patch.object(task,'_connect',return_value=c):task.load_rules()
 sql=q.execute.call_args.args[0]
 assert 'r.knowledge_version=m.knowledge_version' in sql and "binding_status='mapped'" in sql
 assert 'ORDER BY id DESC LIMIT 1' in sql and 'INSERT' not in sql


def test_health_issue_aliases_negation_and_urgent_bypass():
 c,q=connection([ISSUE])
 with patch.object(task,'_connect',return_value=c):
  assert task.identify_issues('猫咪便便臭是为什么')[0]['issue_code']=='STOOL_ODOR'
  assert task.identify_issues('没有便臭')==[]
 with patch.object(task,'identify_issues',return_value=[{**ISSUE,'high_risk_warning':1}]),patch.object(task,'health_rows') as lookup:
  assert task.health_answer(dict(query='便血',species='cat'),consultation=True)['status']=='risk_alert';lookup.assert_not_called()


def test_health_empty_and_failed_synthesis_never_fallback_to_food_knowledge():
 plan=dict(query='便臭',species='cat')
 with patch.object(task,'identify_issues',return_value=[ISSUE]),patch.object(task,'health_rows',return_value=[]):
  assert task.health_answer(plan)['status']=='no_match'
 with patch.object(task,'identify_issues',return_value=[ISSUE]),patch.object(task,'health_rows',return_value=[{'id':1}]),patch.object(task,'summarize_health',side_effect=ValueError('bad')):
  result=task.health_answer(plan);assert result['status']=='unavailable' and not result['groups']
 state={'slots':{},'turn_count':0}
 with patch.object(task,'health_answer',return_value=result),patch.object(qa,'retrieve') as lookup:
  response=qa.handle_question('u',{'pet_id':None,'profile_species':'cat'},'便臭',state);lookup.assert_not_called()
 assert response['result']['pipeline']=='health_knowledge' and state['turn_count']==1
 assert state['slots'][qa.STATE_KEY]['source_table']=='health_knowledge_point'


def test_model_output_schema_citations_and_no_dosing():
 client=MagicMock();response=client.chat.completions.create.return_value
 with patch.object(task,'get_chat_model_config',return_value=dict(api_key='test',base_url='http://localhost',model='test')),patch.object(task,'OpenAI',return_value=client):
  response.choices[0].message.content=json.dumps({'groups':[GROUP]})
  assert task.summarize_health('便臭',[{'id':1},{'id':2}])==[GROUP]
  for bad in [dict(GROUP,source_ids=[999]),dict(GROUP,source_ids=[True]),dict(GROUP,mechanism='服用5毫克药物'),dict(GROUP,manifestations='类型错误')]:
   response.choices[0].message.content=json.dumps({'groups':[bad]})
   with pytest.raises(ValueError):task.summarize_health('便臭',[{'id':1},{'id':2}])


def test_health_saved_groups_sources_and_random_question_pool():
 rows=[dict(id=1,batch_id=8,source_provider='qwen',source_model='qwen',review_status='pending')]
 groups=[dict(GROUP,cause='原因'+str(i),source_ids=[1]) for i in range(7)]
 with patch.object(task,'identify_issues',return_value=[ISSUE]),patch.object(task,'health_rows',return_value=rows),patch.object(task,'summarize_health',return_value=groups):
  result=task.health_answer(dict(query='猫咪便便臭',species='cat'))
 saved=json.loads(json.dumps(result));assert saved['groups']==groups and saved['page_size']==3
 assert saved['sources']==rows and len(set(saved['questions']))==6


def test_named_switch_order():
 assert flow.named_request('从皇家F32换到卫仕LA40有什么影响？')=='皇家F32和卫仕LA40'
 assert flow.named_request('对比皇家F32和卫仕LA40')=='皇家F32和卫仕LA40'


def test_switch_impact_routes_to_comparison_not_new_food_recommendation():
 from services import miniprogram_chat_service as chat
 decision,_=chat._recognize_guardian_turn('帮我分析从皇家F32猫粮换到卫仕LA40猫粮有什么影响？',{'slots':{}})
 assert decision['primary_intent']=='food_analysis'
 assert decision['slots']['food_name']=='皇家F32猫粮和卫仕LA40猫粮'


def test_health_outage_preserves_health_boundary_and_rule_availability():
 import pymysql
 with patch.object(task,'identify_issues',side_effect=pymysql.OperationalError('offline')):
  assert task.health_answer(dict(query='猫咪便便臭',species='cat'))['status']=='unavailable'
 with patch.object(task,'load_rules',return_value=[rule(1,'fiber_support','fiber','increase',True)]),patch.object(task,'switch_health_rows',side_effect=pymysql.OperationalError('offline')):
  r=task.nutrition_pairs([pair([dim('fiber',1,3,5)])],'cat')
 assert r['status']=='ready' and not r['pairs'][0]['products'][1]['strengths']
 assert '暂时无法读取' in r['pairs'][0]['health_note']


def test_soft_stool_health_supplement_keeps_original_bubble_and_turn():
 from services import miniprogram_chat_service as chat
 original={'state':{'turn':1,'slots':{},'slot_status':{},'analysis':{}},'status':'answered','reply':'原有分析','reply_parts':[{'response_type':'text','reply':'原有分析'}],'actions':[]}
 supplement={'reply':'健康资料','result':{'pipeline':'health_knowledge','health_knowledge':{'version':1,'status':'matched','groups':[GROUP]}}}
 state=chat._default_state('c')
 with patch.object(chat,'handle_soft_stool_turn',return_value=original),patch.object(qa,'handle_question',return_value=supplement):
  r=chat._handle_soft_stool_message('u',{'id':'c','pet_id':'p'},'都说明了',state,'m')
 assert r['messages'][0]['reply']=='原有分析' and 'health_knowledge' not in r['messages'][0]['result']
 assert r['messages'][-1]['result']['pipeline']=='health_knowledge'
 assert r['messages'][-1]['result']['turn_no']==state['turn_count']==1


def test_report_retains_switch_plan_and_marks_version_mismatch():
 import test_guardian_report as fixture
 from services import guardian_report_service as report
 p1=fixture.profile();p2=fixture.profile(2,fiber_score=4)
 subjects=[dict(formula_id=1,label='A',portrait=p1['six_dimensions']),dict(formula_id=2,label='B',portrait=p2['six_dimensions'])]
 plan=dict(steps=['逐步引入目标粮'],confirmation='已开始吃再确认')
 with patch.object(report,'load_profiles',return_value={1:p1,2:p2}),patch.object(report,'community_evidence',return_value=fixture.EMPTY_COMMUNITY),patch.object(task,'load_rules',return_value=[]),patch.object(task,'health_rows',return_value=[]):
  r=report.attach_report({'result':{'switch_plan':plan}},{'pet_profile':{'animal_type':'cat'}},subjects,scene='switch_impact')['result']['guardian_report']
  assert r['next_steps']['switch_plan']==plan and r['scene']=='switch_impact'
  subjects[1]['portrait']['calculation_version']='other'
  r=report.attach_report({'result':{}},{'pet_profile':{'animal_type':'cat'}},subjects)['result']['guardian_report']
  assert '版本不一致' in r['summary']['text']
  assert not r['feeding']['task_nutrition']['pairs'][0]['top_changes']
