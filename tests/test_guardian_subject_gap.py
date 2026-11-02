"""Turn-level regression tests for requirements 40 and 41."""
import json
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import MagicMock, patch

from services import guardian_knowledge_service as qa
from services import guardian_subject_service as subjects
from services import miniprogram_chat_service as chat


class SubjectGapTest(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.conv = {'id': 'c', 'pet_id': 'cat-id', 'profile_species': 'cat', 'profile_name': '胖胖', 'profile_age': 36}
        self.state = chat._default_state('c')
        self.seq = 0
        self.mock(chat, '_get_conversation', return_value=self.conv)
        self.mock(chat, '_load_state', side_effect=lambda _: self.state)
        self.mock(chat, '_save_state', side_effect=lambda state: setattr(self, 'state', state))
        self.mock(chat, '_save_message', side_effect=self.save)
        self.mock(chat, '_connect', return_value=MagicMock())
        self.mock(chat, 'get_chat_model_config', return_value={'api_key': ''})
        self.profile = self.mock(qa, 'load_context', return_value={
            'pet_profile': {'animal_type': 'cat', 'name': '胖胖', 'age_months': 36},
            'health_anomalies': {'status': 'not_requested'}, 'diet_history_status': 'not_requested'})
        self.retrieve = self.mock(qa, 'retrieve', return_value={'status': 'no_match', 'items': []})
        self.mock(qa, 'get_chat_model_config', return_value={'api_key': 'test', 'base_url': 'test', 'model': 'test'})
        self.model = self.mock(qa, 'OpenAI').return_value.chat.completions.create
        self.advice = {'answerable': True, 'conclusion': '给它安静休息的空间。', 'actions': ['备好清水。']}
        self.model.return_value = self.response(self.advice)

    def mock(self, target, attr, **opts):
        return self.stack.enter_context(patch.object(target, attr, **opts))

    def save(self, **kwargs):
        self.seq += 1
        return {'id': str(self.seq), **kwargs}

    def response(self, payload):
        response = MagicMock()
        response.choices[0].message.content = json.dumps(payload, ensure_ascii=False)
        return response

    def turn(self, message='', **kwargs):
        return chat.handle_message('owner', 'c', {'message': message, **kwargs})['message']

    def test_other_dog_followup_then_explicitly_return_to_cat(self):
        self.state.update(primary_intent='food_switch', current_step='food_goal')
        for question in ('我家有猫，小狗到家要注意什么', '那它晚上叫怎么办'):
            reply = self.turn(question)
            self.assertEqual('dog', self.retrieve.call_args.args[0]['species'])
            sent = json.loads(self.model.call_args.kwargs['messages'][1]['content'])
            self.assertEqual({'animal_type': 'dog'}, sent['profile'])
            self.assertNotIn('胖胖', str(sent))
            meta = reply['result']['knowledge_qa']
            self.assertEqual(('no_match', 'model_general_advice', []),
                             (meta['status'], meta['generation_source'], meta['sources']))
        self.profile.assert_not_called()
        self.assertIn('到家', sent['query'])
        self.turn('我家胖胖平时怎么刷牙')
        self.profile.assert_called_once()
        self.assertEqual('cat', self.retrieve.call_args.args[0]['species'])
        self.assertNotIn('到家', self.retrieve.call_args.args[0]['query'])

    def test_same_species_and_diet_shortcut_never_read_bound_history(self):
        diet = self.mock(chat, '_handle_diet_advice_entry')
        self.turn('朋友家的猫需要什么喂养建议', client_context={'entry_point': 'diet_advice'})
        self.profile.assert_not_called()
        diet.assert_not_called()
        self.assertEqual('cat', self.state['slots']['_external_subject'])
        self.assertEqual('profile_required', self.turn('明天提醒我喂朋友家的猫')['result']['status'])
        self.assertEqual('risk_alert', self.turn('它便血了')['response_type'])
        self.assertEqual(1, self.model.call_count)

    def test_confirmation_continues_original_question_and_rejects_stale_button(self):
        self.turn('小狗怎么适应新家')
        prompt = self.turn('它最近吃的粮怎么样')
        option = {'slot': chat.SUBJECT_SCOPE_SLOT, 'value': prompt['interaction']['options'][1]['value']}
        self.turn(interaction=option)
        self.assertIn('它最近吃的粮', self.retrieve.call_args.args[0]['query'])
        self.assertEqual('cat', self.retrieve.call_args.args[0]['species'])
        with self.assertRaises(ValueError):
            self.turn(interaction=option)

    def test_matched_but_insufficient_uses_general_answer_without_sources(self):
        row = {'id': 8, 'content': '无关资料', 'status': '已审核', 'evidence_status': '专业已验证'}
        self.retrieve.return_value = {'status': 'matched', 'items': [row]}
        self.model.side_effect = [self.response({'sufficient': False, 'data': []}), self.response(self.advice)]
        reply = self.turn('小狗新家怎么布置')
        meta = reply['result']['knowledge_qa']
        self.assertEqual(('insufficient_evidence', [8], [], '1'),
                         (meta['status'], meta['candidate_guide_ids'], meta['guide_ids'], meta['gap_message_id']))
        self.assertIn('AI 通用养宠建议', reply['content'])
        self.assertNotIn('无关资料', str(self.model.call_args.kwargs['messages']))

    def test_unavailable_is_not_a_gap_and_model_failure_is(self):
        self.retrieve.return_value = {'status': 'unavailable', 'items': []}
        reply = self.turn('小狗怎么陪玩')
        self.assertNotIn('gap_message_id', reply['result']['knowledge_qa'])
        self.model.assert_not_called()
        self.retrieve.return_value = {'status': 'no_match', 'items': []}
        self.model.side_effect = TimeoutError()
        meta = self.turn('那它晚上呢')['result']['knowledge_qa']
        self.assertEqual(('3', 'model_unavailable'), (meta['gap_message_id'], meta['generation_source']))

    def test_mixed_species_pronoun_and_legacy_kitten_choice(self):
        self.turn('猫和狗如何相处')
        prompt = self.turn('它怎么刷牙')
        choice = prompt['interaction']['options'][1]
        self.turn(interaction={'slot': chat.SUBJECT_SCOPE_SLOT, 'value': choice['value']})
        self.assertEqual('dog', self.retrieve.call_args.args[0]['species'])
        self.assertNotIn('猫和狗', self.retrieve.call_args.args[0]['query'])
        self.profile.assert_not_called()
        self.state = chat._default_state('c')
        self.state['current_step'] = chat.SUBJECT_SCOPE_SLOT
        self.state['slots']['_subject_scope_question'] = '幼猫如何磨爪'
        self.turn(interaction={'slot': chat.SUBJECT_SCOPE_SLOT, 'value': 'general_kitten'})
        self.assertEqual({'animal_type': 'cat'}, self.retrieve.call_args.args[0]['pet_scope'])
        self.profile.assert_not_called()

    def test_invalid_or_sensitive_general_advice_never_reaches_user(self):
        for text in ('小狗该吃什么药', '小狗这款粮的蛋白含量'):
            self.assertEqual('scope_limited', self.turn(text)['result']['knowledge_qa']['generation_source'])
        self.model.assert_not_called()
        self.model.return_value = self.response({**self.advice, 'actions': ['错' * 201]})
        self.assertNotIn('错', self.turn('小狗如何磨牙')['content'])

    def test_supplies_and_other_food_are_not_a_new_animal(self):
        for question in ('我家的猫能吃狗粮吗', '胖胖喜欢其他口粮', '朋友建议我家的猫多玩'):
            self.assertIsNone(subjects.resolve(question, self.conv, None))
        self.assertEqual('dog', subjects.resolve('帮我选一款狗粮', self.conv, None))
        self.assertEqual('cat', subjects.resolve('朋友家的猫刚到，猫怎么磨爪', self.conv, None))
        self.assertEqual('both', subjects.resolve('猫和狗怎么相处', self.conv, None))

    def test_irrelevant_history_is_not_read_for_general_care(self):
        self.stack.close()
        with patch.object(qa, 'get_cat_profile', return_value={'animal_type': 'cat'}), \
             patch.object(qa, 'list_diet_history') as diet, patch.object(qa, 'load_health') as health:
            context = qa.load_context('u', 'p', question='我家猫怎么磨爪')
        diet.assert_not_called()
        health.assert_not_called()
        self.assertEqual('not_requested', context['diet_history_status'])
