import copy
import unittest
from unittest.mock import patch

from services import guardian_ingredient_assembly_service as service

CATALOG = [
 {'standard_ingredient_id':'fresh','standard_name':'鲜鸡肉','alias_names':'鸡肉、新鲜鸡肉','ingredient_family':'鸡肉类','primary_nutrition_role':'蛋白质供给'},
 {'standard_ingredient_id':'meal','standard_name':'鸡肉粉','alias_names':'脱水鸡肉、鸡粉','ingredient_family':'鸡肉类','primary_nutrition_role':'蛋白质供给'},
 {'standard_ingredient_id':'oil','standard_name':'鱼油','alias_names':'','ingredient_family':'动物油脂类'},
]
FORMULA = {'formula_id':285,'formula_version':2,'ingredient_fingerprint':'same','active_ingredient_fingerprint':'same','status':'active','confirmation_status':'confirmed','is_current':1,'display_name':'测试粮','raw_ingredient_example':'鸡肉粉10%、复合配料6%(鲜鸡肉90%、鱼油5%)'}
ITEMS = [{'item_id':1,'standard_ingredient_id':'meal','raw_name':'鸡肉粉','standard_name':'鸡肉粉','position':1,'review_status':'auto_approved','match_status':'matched','is_ignored':0}]
PROFILE = {'profile_id':7,'profile_version':2,'nutrition_category':'protein','nutrition_subtype':'meal','domain_attributes_json':'{}','function_attributes_json':'{"protein_supply":"strong","omega3_support":"unknown"}','evidence_level':'high'}

class Cursor:
 def __init__(self, formula=None, items=None):
  self.formula=copy.deepcopy(FORMULA if formula is None else formula)
  self.items=copy.deepcopy(ITEMS if items is None else items)
  self.sql=[]
 def execute(self, sql, args=None):
  self.sql.append((sql,args)); self.current=sql
 def fetchall(self):
  return copy.deepcopy(self.items if 'ingredient_item' in self.current else CATALOG)
 def fetchone(self):
  return copy.deepcopy(PROFILE if 'science_profile' in self.current else self.formula)
 def __enter__(self): return self
 def __exit__(self,*a): pass

class Connection:
 def __init__(self,cursor):self.cur=cursor
 def cursor(self):return self.cur
 def __enter__(self):return self
 def __exit__(self,*a):pass

class AssemblyTests(unittest.TestCase):
 def assemble(self, text, cursor=None):
  return service.assemble(285, service.find_targets(text,CATALOG),cursor=cursor or Cursor(),catalog=CATALOG)
 def test_longest_alias_keeps_fresh_and_meal_distinct(self):
  result=service.find_targets('鲜鸡肉和鸡肉粉有什么区别',CATALOG)
  self.assertEqual([x['ids'] for x in result],[['fresh'],['meal']])
 def test_generic_meat_includes_meal_not_oil(self):
  packet=self.assemble('有没有鸡肉')
  self.assertEqual(packet['ingredients'][0]['presence'],'listed')
  self.assertEqual(packet['ingredients'][0]['formula_items'][0]['standard_ingredient_id'],'meal')
 def test_absence_only_when_mapping_complete(self):
  self.assertEqual(self.assemble('鱼油')['ingredients'][0]['presence'],'unresolved') # mentioned in raw compound
  cur=Cursor();cur.formula['raw_ingredient_example']='鸡肉粉10%'
  self.assertEqual(self.assemble('鱼油',cur)['ingredients'][0]['presence'],'not_listed')
  cur.items.append(dict(ITEMS[0],standard_ingredient_id=None,match_status='unmatched',review_status='pending'))
  self.assertEqual(self.assemble('鱼油',cur)['ingredients'][0]['presence'],'unresolved')
 def test_stale_mapping_never_confirms_presence(self):
  cur=Cursor();cur.formula['active_ingredient_fingerprint']='changed'
  self.assertEqual(self.assemble('鸡肉粉',cur)['ingredients'][0]['presence'],'unresolved')
 def test_unconfirmed_formula_never_confirms_presence(self):
  cur=Cursor();cur.formula['confirmation_status']='pending'
  self.assertEqual(self.assemble('鸡肉粉',cur)['ingredients'][0]['presence'],'unresolved')
 def test_label_percent_keeps_compound_scope(self):
  result=service.label_amounts(FORMULA['raw_ingredient_example'],['鲜鸡肉','鸡肉粉','鸡肉'])
  self.assertCountEqual([(x['percent'],x['scope']) for x in result],[('90','compound_subingredient'),('10','whole_formula_label')])
  self.assertEqual(service.label_amounts('鸡肉粉10%',['鸡肉']),[])
 def test_profile_selection_is_versioned_and_read_only(self):
  cur=Cursor();self.assemble('鸡肉粉',cur)
  sql=[sql for sql,args in cur.sql if 'science_profile' in sql][0]
  self.assertIn("science_status='active'",sql)
  self.assertIn('ORDER BY profile_version DESC',sql)
  self.assertTrue(all(sql.startswith('SELECT ') for sql,args in cur.sql))
 def test_alias_collision_requires_clarification(self):
  catalog=copy.deepcopy(CATALOG);catalog[2]['alias_names']='鸡粉'
  targets=service.find_targets('鸡粉',catalog)
  packet=service.assemble(285,targets,cursor=Cursor(),catalog=catalog)
  self.assertEqual(packet['ingredients'][0]['presence'],'ambiguous')
 def test_render_does_not_promote_profile_into_actual_effect(self):
  packet=self.assemble('鸡肉粉和鲜鸡肉')
  text=service.render(packet)
  for expected in ['真实添加理由仍需厂家','不能算出贡献克数','不能判断哪一种更好','标签标示比例']:
   self.assertIn(expected,text)
  self.assertNotIn('提供 Omega-3',text)
 def test_routing_exclusions_and_followup(self):
  state={'slots':{service.SLOT:{'pet_id':1,'formula_id':285,'targets':[{'query':'鸡肉粉','ids':['meal']}]}}}
  for text in ['当前吃的粮有没有鸡肉粉','现在吃的粮中的鱼油是什么','它的添加量呢','鸡肉粉和鲜鸡肉有什么区别']:
   self.assertTrue(service.is_request(text,state,1),text)
  for text in ['当前粮和历史粮哪个好','刚上传的粮有什么成分','推荐一个粮','EP42有什么配料','另一款粮的作用']:
   self.assertFalse(service.is_request(text,state,1),text)
  self.assertFalse(service.is_request('它有什么作用',state,2))
  self.assertFalse(service.is_request('这款粮有没有鸡肉粉',{},1))
 def test_switch_formula_does_not_reuse_target(self):
  state={'slots':{service.SLOT:{'pet_id':1,'formula_id':284,'targets':[{'query':'鱼油','ids':['oil']}]}}}
  with patch.object(service,'_connect',return_value=Connection(Cursor())):
   reply=service.handle_question('它有什么作用',{'current_food':{'formula_id':285}},state,1)
  self.assertEqual(reply['result']['ingredient_evidence']['status'],'ingredient_needed')
  self.assertNotIn(service.SLOT,state['slots'])
 def test_unknown_explicit_target_never_reuses_previous(self):
  state={'slots':{service.SLOT:{'pet_id':1,'formula_id':285,'targets':[{'query':'鱼油','ids':['oil']}]}}}
  with patch.object(service,'_connect',return_value=Connection(Cursor())):
   reply=service.handle_question('当前粮有没有未收录原料',{'current_food':{'formula_id':285}},state,1)
  self.assertEqual(reply['result']['ingredient_evidence']['status'],'ingredient_needed')
 def test_database_failure_has_no_fabricated_answer(self):
  import pymysql
  with patch.object(service,'_connect',side_effect=pymysql.OperationalError('unavailable')):
   with self.assertLogs(service.__name__,level='ERROR'):
    reply=service.handle_question('当前粮有没有鱼油',{'current_food':{'formula_id':285}},{},1)
  self.assertEqual(reply['result']['ingredient_evidence']['status'],'evidence_unavailable')
 def test_missing_formula_does_not_fuzzy_resolve(self):
  with patch.object(service,'_connect',side_effect=AssertionError('must not query')):
   reply=service.handle_question('当前粮有没有鸡肉粉',{'current_food':{'product_name':'同名粮'}},{},1)
  self.assertEqual(reply['result']['ingredient_evidence']['status'],'current_formula_missing')
 def test_own_state_preserves_workflows(self):
  state={'slots':{'other_workflow':{'id':1}},'turn_count':3,'current_step':'existing'}
  with patch.object(service,'_connect',return_value=Connection(Cursor())):
   reply=service.handle_question('当前粮有没有鸡肉粉',{'current_food':{'formula_id':285}},state,1)
  self.assertEqual(reply['response_type'],'text')
  self.assertEqual(state['slots']['other_workflow'],{'id':1})
  self.assertEqual(state['current_step'],'existing')
  self.assertEqual(state['turn_count'],4)

if __name__=='__main__':unittest.main()
