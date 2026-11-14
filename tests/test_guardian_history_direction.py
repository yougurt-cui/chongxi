from unittest.mock import patch
from services import guardian_report_service as report


def products():
 def p(i,label,roles,score):
  return dict(id=i,label=label,roles=roles,status='ready',feature_version='f',portrait_version='v',calculation_version='c',dimensions=[dict(key='fat',title='脂肪负担',score=score,scale_max=1,direction='higher_is_burden')])
 return [p('current','卫仕 LA40',['current'],.21),p('history','皇家 F32',['history'],.581)]


def test_history_to_current_reverses_scores_and_ids_without_mutating_display_order():
 ps=products();pair=report.compare_products(ps,history_to_current=True)[0]
 assert (pair['left_id'],pair['right_id'])==('history','current')
 assert pair['dimensions'][0]['delta']==-.371
 assert ps[0]['id']=='current'
 assert report.compare_products(ps)[0]['dimensions'][0]['delta']==.371


def test_multiple_histories_compare_independently_to_current():
 ps=products();ps.append({**ps[1],'id':'older','label':'另一历史粮'})
 pairs=report.compare_products(ps,history_to_current=True)
 assert [(p['left_id'],p['right_id']) for p in pairs]==[('history','current'),('older','current')]


def test_report_scope_drives_history_direction_but_preserves_explicit_switch():
 from services import guardian_task_knowledge as task
 empty=dict(status='no_match',items=[],source_status={},note='')
 for scope,scene,expected in [('current_vs_history',None,('history','current')),('saved_diet',None,('history','current')),('named_comparison','switch_impact',('current','history'))]:
  with patch.object(report,'load_profiles',return_value={}),patch.object(report,'product_evidence',return_value=products()),patch.object(report,'community_evidence',return_value=empty),patch.object(task,'nutrition_pairs',return_value={'pairs':[]}),patch.object(task,'question_pool',return_value=[]):
   r=report.attach_report({'result':{}},{'food_analysis_scope':scope},scene=scene)['result']['guardian_report']
  pair=r['feeding']['comparisons'][0]
  assert (pair['left_id'],pair['right_id'])==expected
