"""Live-orchestrator routing regression; older local orchestrators skip this suite."""
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from services import miniprogram_chat_service as chat
from services import guardian_ingredient_assembly_service as ingredient

@unittest.skipUnless(hasattr(chat, '_resolve_scene'), 'requires deployed scene-aware orchestrator')
class IngredientChatRoute(unittest.TestCase):
 def setUp(self):
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  self.conv={'id':'c','pet_id':'cat','profile_species':'cat','profile_name':'测试猫','profile_age':36}
  self.state=chat._default_state('c');self.sequence=0
  self.mock(chat,'_get_conversation',return_value=self.conv)
  self.mock(chat,'_load_state',return_value=self.state)
  self.mock(chat,'_save_state')
  self.mock(chat,'_connect',return_value=MagicMock())
  self.mock(chat,'_save_message',side_effect=self.save)
  self.scene=self.mock(chat,'_resolve_scene',return_value=(None,[],None))
  self.mock(chat.health_flow,'confirmation_interaction',return_value=None)
  self.context=self.mock(chat,'_build_context',return_value={'current_food':{'formula_id':285}})
  self.assemble=self.mock(ingredient,'handle_question',side_effect=lambda m,c,s,p: ingredient._reply('已核对配方',s,{'status':'ok','formula_id':285}))
  self.recognize=self.mock(chat,'_recognize_guardian_turn',side_effect=AssertionError('must not call intent model'))
 def mock(self,obj,name,**kwargs):return self.stack.enter_context(patch.object(obj,name,**kwargs))
 def save(self,**kwargs):
  self.sequence+=1;return {'id':str(self.sequence),**kwargs}
 def turn(self,text):return chat.handle_message('owner','c',{'message':text})['message']
 def test_current_food_ingredient_takes_precedence_and_persists_evidence(self):
  reply=self.turn('当前吃的粮有没有鸡肉粉，为什么添加？')
  self.assertEqual(reply['result']['pipeline'],'current_food_ingredient_assembly')
  self.assertEqual(self.assemble.call_args.args[3],'cat')
  self.assertFalse(self.scene.call_args.kwargs['allow_text'])
  self.recognize.assert_not_called()
 def test_explicit_current_food_exits_failed_upload_guide(self):
  self.state['slots'][chat.ingredient_guide.SLOT]={'pet_id':'cat','failure_node':'ocr_formula'}
  advance=self.mock(chat.ingredient_guide,'advance',side_effect=AssertionError('must use current food'))
  reply=self.turn('当前口粮有没有鱼油？')
  self.assertEqual(reply['result']['pipeline'],'current_food_ingredient_assembly')
  advance.assert_not_called()
 def test_urgent_question_keeps_emergency_route(self):
  self.scene.return_value=({'task':'health_consult','needs':[]},[],None)
  route=self.mock(chat,'_scene_reply',return_value={'reply':'立即就医','response_type':'risk_alert','result':{}})
  reply=self.turn('当前口粮里的鸡肉粉是什么，猫呼吸困难了')
  self.assemble.assert_not_called();route.assert_called_once()
  self.assertEqual(reply['response_type'],'risk_alert')
