"""Requirement 30: shortcut routing uses feeding facts, including incomplete records."""
from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch

from services import miniprogram_chat_service as chat
from services import guardian_health_flow_service as flow
from services import guardian_health_summary_service as summary
import test_guardian_health_analysis as health_fixtures


class DietAdviceTest(unittest.TestCase):
    def setUp(self):
        health_fixtures.HealthChatTest.setUp(self)
        self.context['diet_history_status'] = 'available'
        mock = patch.object(summary, 'get_chat_model_config', return_value={'api_key': ''})
        mock.start(); self.addCleanup(mock.stop)

    def enter(self):
        return chat._handle_diet_advice_entry('u1', self.conversation, self.state, 'm1', {'entry_point': 'diet_advice'})

    def test_current_and_history_have_roles_overview_and_no_forced_comparison(self):
        with patch.object(chat, 'recommend_guardian_foods') as recall:
            result = self.enter()
        recall.assert_not_called()
        report = result['result']['health_analysis']
        self.assertEqual([r['formula_id'] for r in report['subjects']], [1, 2])
        self.assertEqual([r['roles'] for r in report['subjects']], [['current'], ['history']])
        self.assertTrue(report['overview'])
        self.assertEqual(report['comparisons'], [])
        self.assertEqual(result['interaction']['slot'], flow.RECOMMEND_SLOT)

    def test_current_only_does_not_ask_whether_known_current_food_was_eaten(self):
        self.context['diet_history'] = []
        result = self.enter()
        self.assertEqual(len(result['result']['health_analysis']['subjects']), 1)
        self.assertEqual(result['interaction']['slot'], flow.RECOMMEND_SLOT)
        self.assertNotIn('已经在吃', result['reply'])
        declined = flow.accept_confirmation({'slot': flow.RECOMMEND_SLOT, 'value': result['interaction']['options'][1]['value']},
                                           'u1', self.conversation, self.state)
        self.assertEqual(declined['result']['analysis_stage'], 'declined')

    def test_history_only_has_no_invented_current_food(self):
        self.context['current_food'] = None
        result = self.enter()
        subjects = result['result']['health_analysis']['subjects']
        self.assertEqual([r['formula_id'] for r in subjects], [2])
        self.assertEqual(subjects[0]['roles'], ['history'])
        self.assertNotIn('当前粮（未记录）', result['reply'])

    def test_single_history_yes_enters_existing_recommendation_without_marking_it_current(self):
        self.context['current_food'] = None
        result = self.enter()
        with patch.object(chat, 'recommend_guardian_foods') as recall:
            reply = chat._handle_context_message('u1', self.conversation, '', self.state, interaction={
                'slot': flow.RECOMMEND_SLOT, 'value': result['interaction']['options'][0]['value']})
        recall.assert_not_called()
        self.assertEqual(reply['interaction']['slot'], 'food_health_state')
        self.assertIsNone(self.context['current_food'])
        self.assertEqual(self.state['slots'][flow.RECOMMENDATION_KEY]['subjects'][0]['roles'], ['history'])

    def test_repeated_formula_deduplicates_and_preserves_both_roles(self):
        self.context['diet_history'][0]['formula_id'] = 1
        self.context['diet_history'].append(deepcopy(self.context['diet_history'][0]))
        result = self.enter()
        subjects = result['result']['health_analysis']['subjects']
        self.assertEqual(len(subjects), 1)
        self.assertEqual(subjects[0]['roles'], ['current', 'history'])

    def test_unlinked_and_missing_portrait_are_kept_as_analysis_objects(self):
        self.context['diet_history'].append({'brand': '手动牌', 'product': '手动粮', 'formula_id': None})
        with patch.object(chat, '_food_analysis_by_formula', return_value=None):
            result = self.enter()
        subjects = result['result']['health_analysis']['subjects']
        self.assertEqual(len(subjects), 3)
        self.assertTrue(all(r['reason'] == 'portrait_unavailable' for r in subjects))
        self.assertIn('手动牌 手动粮', result['reply'])

    def test_current_diet_history_record_is_not_labelled_as_past(self):
        self.context['current_food'] = None
        self.context['diet_history'][0]['is_current'] = True
        result = self.enter()
        self.assertEqual(result['result']['health_analysis']['subjects'][0]['roles'], ['current'])

    def test_no_records_falls_back_and_uploads_do_not_count_as_eaten(self):
        self.context.update(current_food=None, diet_history=[], diet_history_status='no_records',
                            food_list=[{'brand': '只上传过', 'product_name': '没有吃过'}])
        before = deepcopy(self.state)
        self.assertIsNone(self.enter())
        self.assertEqual(self.state, before)

    def test_history_read_failure_does_not_claim_no_records(self):
        self.context.update(current_food=None, diet_history=[], diet_history_status='unavailable')
        result = self.enter()
        self.assertEqual(result['result']['status'], 'diet_records_unavailable')

    def test_current_only_when_history_temporarily_unavailable_has_coverage_note(self):
        self.context.update(diet_history=[], diet_history_status='unavailable')
        self.assertIn('历史口粮暂时无法读取', self.enter()['reply'])

    def test_history_truncation_is_disclosed_and_old_choice_is_invalidated(self):
        self.context['diet_history_has_more'] = True
        old = self.enter()
        new = self.enter()
        self.assertIn('最近10条', new['reply'])
        with self.assertRaises(ValueError):
            flow.accept_confirmation({'slot': flow.RECOMMEND_SLOT, 'value': old['interaction']['options'][0]['value']},
                                     'u1', self.conversation, self.state)

    def test_explicit_entry_routes_before_intent_recognition_and_persists_report(self):
        def save(**values):
            return {'id': 'message', **values}
        with patch.object(chat, '_get_conversation', return_value=self.conversation), \
             patch.object(chat, '_load_state', return_value=self.state), \
             patch.object(chat, '_save_message', side_effect=save), \
             patch.object(chat, '_save_state'), patch.object(chat, '_connect', return_value=MagicMock()), \
             patch.object(chat, '_recognize_guardian_turn') as recognize:
            result = chat.handle_message('u1', 'c1', {'message': '想了解适合我家宠物的日常饮食建议。',
                                                    'client_context': {'entry_point': 'diet_advice'}})
        recognize.assert_not_called()
        self.assertEqual(result['message']['result']['intent'], 'food_analysis')


if __name__ == '__main__':
    unittest.main()
