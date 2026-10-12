from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from services import guardian_food_analysis_service as analysis
from services import guardian_recommendation_service as recommendations
from services import guardian_reminder_flow_service as reminder_flow
from services import miniprogram_reminder_service as reminders
from services import miniprogram_chat_service as chat
from api.miniprogram_api import miniprogram_api


class NutritionTests(unittest.TestCase):
    def metric(self, value=30, **kwargs):
        return {'name': '粗蛋白', 'value': value, 'unit': '%', 'basis': 'as_fed', 'operator': '>=', **kwargs}

    def test_label_limits_are_compared_without_claiming_real_amounts(self):
        result = analysis.nutrient_comparison({'粗蛋白': self.metric()}, {'粗蛋白': self.metric(35)})[0]
        self.assertEqual(result['delta'], 5)
        self.assertIn('上下限不代表实际', result['note'])

    def test_basis_units_conflicts_and_missing_are_not_subtracted(self):
        for candidate in ({}, {'value': None}, self.metric(basis='dry_matter'), self.metric(unit='g/kg'), self.metric(operator='<=')):
            self.assertIsNone(analysis.nutrient_comparison({'粗蛋白': self.metric()}, {'粗蛋白': candidate})[0]['delta'])

    def test_empty_risk_data_is_not_no_risk(self):
        result = analysis.analyse_food({'formula_id': 1})
        self.assertEqual(result['risk_status'], 'insufficient_evidence')
        self.assertTrue(all(row['value'] is None for row in result['nutrition']))

    def test_repository_marks_conflicting_sources(self):
        connection = MagicMock(); c = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        c.fetchall.side_effect = [[{'formula_id': 1, 'metric_name': '粗蛋白', 'metric_value': 30, 'metric_unit': '%', 'basis': 'as_fed', 'operator_symbol': '>='},
                                  {'formula_id': 1, 'metric_name': '粗蛋白', 'metric_value': 35, 'metric_unit': '%', 'basis': 'as_fed', 'operator_symbol': '>='}], []]
        with patch.object(analysis.pymysql, 'connect', return_value=connection):
            result = analysis.load_nutrition([1])
        self.assertIsNone(result[1]['粗蛋白']['value'])
        self.assertEqual(result[1]['粗蛋白']['status'], 'conflicting')

    def test_history_excludes_current_and_prior_versions_but_not_unrelated(self):
        options = [{'formula_id': 1, 'product_id': 10, 'brand': '甲', 'product_name': 'A'},
                   {'formula_id': 2, 'product_id': 20, 'brand': '乙', 'product_name': 'B'},
                   {'formula_id': 3, 'product_id': 20, 'brand': '乙', 'product_name': 'B2'},
                   {'formula_id': 4, 'product_id': 40, 'brand': '丙', 'product_name': 'C'}]
        excluded = analysis.excluded_history_ids({'food_formula_id': 1}, [{'formula_id': 2}], options)
        self.assertEqual(excluded, {1, 2, 3})

    def test_recall_is_versioned_and_does_not_reintroduce_excluded_foods(self):
        import pandas as pd
        rows = [{'formula_id': n, **{key: value for key in analysis.SIMILARITY_WEIGHTS}} for n, value in [(1, 10), (2, 11), (3, 80)]]
        with patch.object(pd, 'read_sql', return_value=pd.DataFrame(rows)) as reader:
            result = analysis.recall_similar(MagicMock(), 1, {3})
        self.assertEqual([row['formula_id'] for row in result['items']], [3])
        self.assertEqual(reader.call_args.kwargs['params'], {'version': 'soft_v1'})

    def test_ambiguous_weight_does_not_default_to_weight_loss(self):
        self.assertIsNone(recommendations.recommendation_goal({'switch_reason': 'weight'}))
        self.assertEqual(recommendations.recommendation_goal({'target_requirement': '希望增重'}), 'weight_gain')
        self.assertEqual(recommendations.recommendation_goal({'target_requirement': '美毛'}), 'coat_health')

    def test_uncertain_and_negated_reactions_are_not_negative_experience(self):
        self.assertIsNone(recommendations.reaction_from_note('不知道是否便便正常'))
        self.assertIsNone(recommendations.reaction_from_note('没有过敏'))
        self.assertEqual(recommendations.reaction_from_note('没有过敏，便便正常'), 'good')


class RecommendationIntegrationTests(unittest.TestCase):
    def setUp(self):
        from consumer_apps import product_recommendation_engine as engine
        import pandas as pd
        self.engine = engine
        self.options = [{'formula_id': n, 'product_id': n * 10, 'catalog_key': f'formula:{n}', 'brand': '测试牌', 'product_name': f'粮{n}', 'life_stage': 'adult', 'product_type': '干粮', 'ingredient_composition': '鸭肉，米'} for n in (1, 2, 3)]
        rows = [{'formula_id': n, 'product_name': f'粮{n}', 'brand': '测试牌', 'protein_quality': score,
                 **{key: 65. for key in analysis.FEATURE_LABELS if key != 'protein_quality'}, 'q_feed': 50., 'q_feed_excess_penalty': 0.} for n, score in [(1, 80), (2, 50), (3, 95)]]
        self.pool = pd.DataFrame(rows)
        self.profiles = {'p': {'profile_code': 'p', 'symptom_type': 'soft_stool', 'weights': {'protein_quality': 1}}}
        for mock in [patch('sqlalchemy.create_engine', return_value=MagicMock()), patch.object(engine, 'load_recommendation_profiles', return_value=self.profiles),
                     patch.object(engine, 'load_product_pool', return_value=self.pool), patch.object(engine, 'add_derived_features', side_effect=lambda frame: frame),
                     patch.object(recommendations, 'list_standardized_product_options', return_value={'items': self.options}),
                     patch.object(recommendations, 'load_nutrition', return_value={}), patch.object(recommendations, 'load_risks', return_value={})]:
            mock.start(); self.addCleanup(mock.stop)
        self.pet = {'animal_type': 'cat', 'age_months': None, 'food_formula_id': 2}

    def test_current_and_history_are_excluded_before_similarity_recall(self):
        with patch.object(recommendations, 'recall_similar', return_value={'status': 'ready', 'items': [{'formula_id': 1, 'similarity': .8}]}) as recall:
            result = recommendations.recommend_guardian_foods(self.pet, {'food_goal': 'soft_stool', '_food_mode': 'switch'}, [{'formula_id': 3, 'is_current': False}])
        self.assertEqual(recall.call_args.args[1:3], (2, {1}))
        self.assertEqual([row['formula_id'] for row in result['items']], [1])
        self.assertEqual(result['items'][0]['similarity'], .8)
        self.assertEqual(result['history_policy'], 'exclude_all_confirmed_foods')

    def test_confirmed_good_feedback_adjusts_profiles_but_topic_counts_do_not(self):
        with patch.object(self.engine, 'derive_history_adjustment_from_features', wraps=self.engine.derive_history_adjustment_from_features) as derive:
            result = recommendations.recommend_guardian_foods(self.pet, {'food_goal': 'soft_stool'}, [{'formula_id': 3, 'product': '粮3', 'note': '以前吃得适应，便便正常'}])
        derive.assert_called_once()
        self.assertTrue(result['items'][0]['history_reasons'])
        with patch.object(self.engine, 'derive_history_adjustment_from_features') as derive:
            recommendations.recommend_guardian_foods(self.pet, {'food_goal': 'soft_stool'}, [{'formula_id': 3, 'health_topics': [{'code': 'digestive', 'conversation_count': 10}]}])
        derive.assert_not_called()

    def test_coat_goal_uses_reference_profile_when_database_has_none(self):
        result = recommendations.recommend_guardian_foods(self.pet, {'food_goal': 'coat_health'})
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(all(row['profile_code'] == 'guardian_coat_reference' for row in result['items']))

    def test_partial_assessment_omits_missing_dimension_instead_of_filling_50(self):
        self.profiles['p']['weights'] = {'protein_quality': .65, 'antioxidant_score': .35}
        self.pool['antioxidant_score'] = None
        result = recommendations.recommend_guardian_foods(self.pet, {'food_goal': 'soft_stool'}, [{'formula_id': 3}])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['items'][0]['fit_score'], 80)
        self.assertEqual(result['assessment_weight_coverage'], .65)
        self.assertIn('antioxidant_score', result['missing_assessment_fields'])
        self.assertNotIn('antioxidant_score', result['items'][0]['analysis']['structure_features'])


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 12, 8, tzinfo=reminders.SHANGHAI)
        self.state = chat._default_state('c')

    def turn(self, message):
        return reminder_flow.handle_reminder('owner', {'pet_id': 'pet'}, message, self.state, {'name': '团团'}, now=self.now)

    def test_complete_request_saves_once_and_reply_follows_write(self):
        with patch.object(reminder_flow, 'create_reminder', return_value={'item': {'id': 'r'}}) as save:
            result = self.turn('明天晚上九点提醒我清洁饮水机')
        self.assertEqual(result['result']['status'], 'saved')
        self.assertEqual(save.call_args.args[1]['date'], '2026-10-13')
        self.assertEqual(save.call_args.args[1]['time'], '21:00')
        self.assertEqual(save.call_args.args[1]['title'], '清洁饮水机')
        self.assertEqual(save.call_args.args[1]['pet_id'], 'pet')
        self.assertIn('不会发送系统推送', result['reply'])

    def test_missing_time_asks_only_time_and_keeps_title_and_day(self):
        with patch.object(reminder_flow, 'create_reminder', return_value={'item': {'id': 'r'}}) as save:
            self.assertEqual(self.turn('明天提醒我买粮')['result']['missing'], 'time')
            save.assert_not_called()
            self.assertEqual(self.turn('晚上九点')['result']['status'], 'saved')
        self.assertEqual(save.call_args.args[1]['title'], '买粮')
        self.assertEqual(save.call_args.args[1]['date'], '2026-10-13')

    def test_failed_write_never_reports_saved(self):
        with patch.object(reminder_flow, 'create_reminder', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError): self.turn('明天9点提醒我买粮')
        self.assertNotEqual(self.state['slots'][reminder_flow.STATE_KEY]['stage'], 'saved')

    def test_past_time_asks_for_future_instead_of_claiming_success(self):
        with patch.object(reminder_flow, 'create_reminder', side_effect=ValueError('提醒时间已经过去')):
            self.assertEqual(self.turn('今天7点提醒我买粮')['result']['status'], 'invalid_time')

    def test_cancel_draft_does_not_delete_existing_reminders(self):
        with patch.object(reminder_flow, 'create_reminder') as save:
            self.turn('明天提醒我买粮')
            self.assertEqual(self.turn('取消')['result']['status'], 'cancelled')
        save.assert_not_called()

    def test_daily_and_weekday_extraction(self):
        for message, day, clock, repeat in [('每天晚上9点提醒我添水', '2026-10-12', '21:00', 'daily'),
                                           ('每周一上午7点提醒我清理猫砂', '2026-10-19', '07:00', 'weekly'),
                                           ('下周三下午三点半提醒我买粮', '2026-10-21', '15:30', None)]:
            values = reminder_flow.extract_request(message, now=self.now)
            self.assertEqual((values['date'], values['time'], values.get('repeat')), (day, clock, repeat))

    def test_capability_question_and_multiple_times_do_not_save(self):
        with patch.object(reminder_flow, 'create_reminder') as save:
            self.assertEqual(self.turn('你能不能提醒我')['result']['missing'], 'title')
            self.assertEqual(self.turn('明天9点买粮，后天10点添水')['result']['status'], 'ambiguous')
            save.assert_not_called()

    def test_date_without_spaces_and_ambiguous_midnight(self):
        values = reminder_flow.extract_request('2027-01-05晚上9点提醒我买粮', now=self.now)
        self.assertEqual(values['date'], '2027-01-05')
        self.assertNotIn('time', reminder_flow.extract_request('明天晚上12点提醒我买粮', now=self.now))

    def test_recurrence_month_end_and_leap_year_anchor(self):
        jan = datetime(2026, 1, 31, 9, tzinfo=reminders.SHANGHAI)
        feb = reminders.next_occurrence(jan, 'monthly', jan, 31, 1)
        self.assertEqual(feb.astimezone(reminders.SHANGHAI).day, 28)
        mar = reminders.next_occurrence(feb, 'monthly', feb, 31, 1)
        self.assertEqual(mar.astimezone(reminders.SHANGHAI).day, 31)
        leap = datetime(2024, 2, 29, 9, tzinfo=reminders.SHANGHAI)
        self.assertEqual(reminders.next_occurrence(leap, 'yearly', leap, 29, 2).astimezone(reminders.SHANGHAI).day, 28)

    def test_routes_are_authenticated_and_ignore_payload_owner(self):
        app = Flask(__name__); app.register_blueprint(miniprogram_api); client = app.test_client()
        self.assertEqual(client.get('/api/miniprogram/reminders').status_code, 401)
        with patch('api.miniprogram_api._current_user_id', return_value='owner'), patch.object(reminders, 'create_reminder', return_value={'ok': True}) as save:
            self.assertEqual(client.post('/api/miniprogram/reminders', json={'user_id': 'attacker'}).status_code, 201)
            self.assertEqual(save.call_args.args[0], 'owner')

    def test_reminder_priority_keeps_health_risk_interrupt(self):
        self.assertEqual(chat._recognize_guardian_turn('明天9点提醒我买粮', self.state)[0]['_target_flow'], 'reminder')
        self.assertEqual(chat._recognize_guardian_turn('便血了，明天提醒我处理', self.state)[0]['_target_flow'], 'soft_stool')
