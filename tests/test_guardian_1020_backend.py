"""October feedback regressions: scoped consent, grounded summaries, records and alternatives."""
from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from api.miniprogram_api import miniprogram_api
from services import miniprogram_chat_service as chat
from services import guardian_health_flow_service as flow
from services import guardian_health_summary_service as summary
from services import guardian_recommendation_presentation as cards
from services import guardian_daily_status_service as status
from services import miniprogram_daily_record_service as daily
from services import miniprogram_diet_history_service as diets
from services import miniprogram_cat_profile_service as profiles
from services import guardian_recommendation_service as recommendations
from services.guardian_presentation import present_message
from services.guardian_food_analysis_service import recommendation_text
from test_guardian_health_analysis import analysis, knowledge
import test_guardian_health_analysis as health_fixtures
from test_guardian_scenario_portraits import features, product


class MultiProductTest(health_fixtures.HealthChatTest):
    def setUp(self):
        super().setUp()
        p = patch.object(summary, 'get_chat_model_config', return_value={'api_key': ''})
        p.start(); self.addCleanup(p.stop)

    def multi(self):
        return self.turn('分析一下现在吃的和历史吃过的粮')

    def test_case4_without_compare_keyword_preserves_both_and_overview_first(self):
        result = self.multi()
        health = result['result']['health_analysis']
        self.assertEqual([r['formula_id'] for r in health['subjects']], [1, 2])
        self.assertEqual([r['roles'] for r in health['subjects']], [['current'], ['history']])
        self.assertTrue(result['reply'].startswith('**猫咪的整体饮食解读**'))
        self.assertIn('是否需要', health['prompt'])
        self.assertNotIn('从历史粮到', health['overview']['text'])
        self.assertEqual(result['interaction']['slot'], flow.RECOMMEND_SLOT)

    def test_legacy_encoded_consent_calls_original_recommendation_only_after_yes(self):
        result = self.multi()
        self.state['slots'].update(food_health_state='none', food_health_stage='none', food_goal='general', _food_mode='selection')
        self.state['slots'].update(_food_flow={'stage': 'selected', 'target': product(3)}, target_food='旧目标')
        with patch.object(chat, 'recommend_guardian_foods', return_value={'status': 'ready', 'items': []}) as recall:
            self.turn(interaction={'slot': flow.RECOMMEND_SLOT, 'value': result['interaction']['options'][0]['value']})
        recall.assert_called_once()
        self.assertEqual(self.state['slots'][flow.RECOMMENDATION_KEY]['stage'], 'confirmed')
        self.assertNotIn('target_food', self.state['slots'])
        with self.assertRaises(ValueError):
            self.turn(interaction={'slot': flow.RECOMMEND_SLOT, 'value': result['interaction']['options'][0]['value']})

    def test_decline_has_no_recall_and_retains_analysis(self):
        result = self.multi()
        with patch.object(chat, 'recommend_guardian_foods') as recall:
            response = self.turn('暂时不用')
        recall.assert_not_called()
        self.assertEqual(response['result']['analysis_stage'], 'declined')
        self.assertEqual(self.state['slots'][flow.STATE_KEY]['analysis_id'], result['result']['analysis_id'])

    def test_wrong_user_pet_conversation_or_confirmation_slot_cannot_recall(self):
        original = self.multi()
        saved = deepcopy(self.state)
        for field, value in (('user_id', 'other'), ('pet_id', 'other'), ('conversation_id', 'other')):
            self.state = deepcopy(saved)
            self.state['slots'][flow.STATE_KEY][field] = value
            with self.assertRaises(ValueError):
                self.turn(interaction={'slot': flow.RECOMMEND_SLOT, 'value': original['interaction']['options'][0]['value']})
        self.state = saved
        with self.assertRaises(ValueError):
            self.turn(interaction={'slot': flow.CONFIRM_SLOT, 'value': original['interaction']['options'][0]['value']})

    def test_duplicate_formula_keeps_roles_but_uses_single_usage_question(self):
        self.context['diet_history'][0]['formula_id'] = 1
        result = self.multi()
        self.assertEqual(len(result['result']['health_analysis']['subjects']), 1)
        self.assertEqual(result['result']['health_analysis']['subjects'][0]['roles'], ['current', 'history'])
        self.assertEqual(result['interaction']['slot'], flow.CONFIRM_SLOT)

    def test_missing_current_portrait_still_lists_current_object(self):
        with patch.object(chat, '_food_analysis_by_formula', side_effect=lambda fid, *args: None if fid == 1 else analysis(fid)):
            result = self.multi()
        subjects = result['result']['health_analysis']['subjects']
        self.assertEqual(subjects[0]['formula_id'], 1)
        self.assertEqual(subjects[0]['reason'], 'portrait_unavailable')
        self.assertEqual(result['result']['health_analysis']['overview']['unavailable_subjects'], ['测试牌 粮1'])

    def test_three_explicit_products_resolve_each_formula(self):
        with patch.object(flow, 'list_standardized_product_options', side_effect=lambda **kw: {'items': [product(i) for i in (1, 2, 3)]}):
            result = self.turn('分析一下测试牌粮1和测试牌粮2和测试牌粮3')
        self.assertEqual([r['formula_id'] for r in result['result']['health_analysis']['subjects']], [1, 2, 3])

    def test_more_history_coverage_is_explicit_and_reverse_order_keeps_current(self):
        self.context['diet_history_has_more'] = True
        result = self.turn('分析一下历史粮和现在吃的')
        self.assertEqual(len(result['result']['health_analysis']['subjects']), 2)
        self.assertIn('最近10条', result['result']['health_analysis']['coverage_note'])


class OverviewTest(unittest.TestCase):
    def reports(self):
        from services.guardian_health_analysis_service import analyse_portrait
        return [{'label': '粮'+str(i), **analyse_portrait(analysis(i)['product_portrait'], knowledge())} for i in (1, 2)]

    def run_model(self, content):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = json.dumps(content)
        with patch.object(summary, 'get_chat_model_config', return_value={'api_key': 'test', 'base_url': 'https://example.com', 'model': 'test', 'provider': 'deepseek'}), patch.object(summary, 'OpenAI', return_value=client):
            result = summary.build_overview(self.reports(), [])
        return result, client

    def test_model_can_select_only_existing_evidence_and_no_free_prose(self):
        result, model = self.run_model({'advantage': ['f0'], 'attention': ['f2'], 'extra': '治愈疾病'})
        # If fixture IDs differ by category the complete response is rejected safely.
        self.assertNotIn('治愈', result['text'])
        model.chat.completions.create.assert_called_once()
        self.assertEqual(result['mode'], 'model_evidence_selection')
        self.assertTrue(result['advantages'])

    def test_fabricated_ids_wrong_categories_and_empty_sections_fall_back(self):
        for data in ({'advantage': ['made-up'], 'attention': []}, {'advantage': [], 'attention': []},
                     {'advantage': {}, 'attention': []}, {'advantage': ['f0', 'f0'], 'attention': []}):
            result, _ = self.run_model(data)
            self.assertEqual(result['mode'], 'evidence_fallback')
            self.assertTrue(result['advantages'])

    def test_provider_failure_still_returns_grounded_report(self):
        with patch.object(summary, 'get_chat_model_config', return_value={'api_key': 'test', 'base_url': 'https://example.com', 'model': 'test'}), patch.object(summary, 'OpenAI', side_effect=TimeoutError):
            result = summary.build_overview(self.reports(), [])
        self.assertEqual(result['mode'], 'evidence_fallback')
        self.assertTrue(result['advantages'])


class RecommendationTest(unittest.TestCase):
    def row(self, fid, brand, score):
        return ({**product(fid), 'brand': brand}, {'score': score}, None)

    def test_diversity_is_bounded_and_formula_deduplicated(self):
        rows = [self.row(1, 'A', 90), self.row(1, 'A', 90), self.row(2, 'A', 89),
                self.row(3, 'B', 87), self.row(4, 'C', 70)]
        chosen = cards.select_candidates(rows)
        self.assertEqual([r[0]['formula_id'] for r in chosen], [1, 3, 2])
        self.assertNotIn(4, [r[0]['formula_id'] for r in chosen])

    def test_no_diversity_jump_over_materially_better_candidate(self):
        chosen = cards.select_candidates([self.row(1, 'A', 95), self.row(2, 'A', 90), self.row(3, 'B', 70)], 2)
        self.assertEqual([r[0]['formula_id'] for r in chosen], [1, 2])

    def test_alternative_requests_are_explicit_not_negated_or_hypothetical(self):
        self.assertTrue(cards.alternatives_requested('换一组'))
        self.assertTrue(cards.alternatives_requested('帮我换一批吧'))
        for text in ('不要换一组', '什么是换一组？', '如果换一组会怎么样', '不需要其他推荐'):
            self.assertFalse(cards.alternatives_requested(text))

    def test_short_text_contains_goal_without_full_nutrition_dump(self):
        item = {**product(1), 'strengths': ['特点1', '特点2', '特点3'], 'cautions': ['年龄未填写，本次未按年龄筛选'],
                'analysis': {'nutrition': ['大量数据']}}
        result = cards.compact_recommendation({'goal': 'weight_loss', 'scenario': {'label': '无明显问题选粮'}, 'items': [item]})
        text = recommendation_text(result)
        self.assertIn('无明显问题＋减重', text)
        self.assertIn('暂无可核验原粮', text)
        self.assertNotIn('大量数据', text)
        self.assertNotIn('特点3', text)
        self.assertIn('未核验能量', text)
        self.assertEqual(len(item['recommendation_detail']['strengths']), 3)

    def test_existing_blocks_and_cards_do_not_duplicate_full_candidate_text(self):
        result = cards.compact_recommendation({'goal': 'weight_loss', 'status': 'ready',
                                               'items': [{**product(1), 'strengths': ['完整候选依据']} ]})
        message = present_message({'role': 'assistant', 'content': recommendation_text(result),
                                   'result': {'food_candidates': result['items'], 'recommendation': result}})
        blocks = json.dumps(message['result']['display_blocks'], ensure_ascii=False)
        self.assertIn('完整候选依据', message['content'])
        self.assertNotIn('完整候选依据', blocks)
        self.assertIn('减重', blocks)

    def test_alternatives_do_not_relax_filters_and_exhaust_cleanly(self):
        rows = {i: features(i, fat_score=.2 + i / 100, carb_score=1.) for i in (1, 2, 3, 4)}
        options = [{**product(i), 'brand': str(i)} for i in rows]
        slots = {'food_health_state': 'none', 'food_goal': 'weight_loss'}
        with patch.object(recommendations, 'list_standardized_product_options', return_value={'items': options}), patch.object(recommendations, 'load_five_features', return_value=rows), patch.object(recommendations, 'load_nutrition', return_value={}), patch.object(recommendations, 'load_risks', return_value={}), patch.object(recommendations, 'load_product_portraits', return_value={}):
            first = recommendations.recommend_guardian_foods({'animal_type': 'cat'}, slots)
            slots['_recommendation_exclude_ids'] = [r['formula_id'] for r in first['items']]
            second = recommendations.recommend_guardian_foods({'animal_type': 'cat'}, slots)
            self.assertEqual(len(second['items']), 1)
            self.assertFalse(set(slots['_recommendation_exclude_ids']) & {r['formula_id'] for r in second['items']})
            slots['_recommendation_exclude_ids'] += [r['formula_id'] for r in second['items']]
            last = recommendations.recommend_guardian_foods({'animal_type': 'cat'}, slots)
            self.assertEqual(last['status'], 'alternatives_exhausted')
            self.assertFalse(last['items'])

    def test_legacy_engine_zero_score_does_not_become_a_new_candidate(self):
        import pandas as pd
        from consumer_apps import product_recommendation_engine as engine
        pool = pd.DataFrame([{'formula_id': 1, 'product_name': 'B', 'brand': 'A', 'protein_quality_score': 0}])
        profiles = {'digest': {'profile_code': 'digest', 'symptom_type': 'soft_stool', 'weights': {'protein_quality': 1}}}
        with patch('sqlalchemy.create_engine', return_value=MagicMock()), patch.object(engine, 'load_recommendation_profiles', return_value=profiles), patch.object(engine, 'load_product_pool', return_value=pool), patch.object(recommendations, 'list_standardized_product_options', return_value={'items': [product(1)]}):
            result = recommendations.recommend_guardian_foods({'animal_type': 'cat'}, {'switch_reason': 'digestive'})
        self.assertFalse(result['items'])


class HistoryIdentityTest(unittest.TestCase):
    def test_selected_history_ids_are_verified_and_names_canonicalized(self):
        resolved = {'standard_brand_name': '真实牌', 'standard_product_name': '真实产品', 'display_name': '真实显示名'}
        with patch.object(profiles, '_resolve_food_formula', return_value=resolved) as resolve:
            data = diets.normalize_history({'product_id': 10, 'formula_id': 20, 'brand': '伪造名', 'product': '伪造产品'})
        resolve.assert_called_once_with(10, 20)
        self.assertEqual((data['brand'], data['product'], data['formula_id']), ('真实牌', '真实显示名', 20))
        for payload in ({'product_id': 1}, {'product_id': True, 'formula_id': 2}, {'product_id': 1, 'formula_id': -1}):
            with self.assertRaises(ValueError):
                diets.normalize_history(payload)

    def test_same_name_different_formula_is_not_deduplicated(self):
        cur = MagicMock()
        cur.fetchall.return_value = [{'brand': 'A', 'product': 'B', 'formula_id': 1}]
        self.assertIsNone(diets._find_duplicate_history(cur, 'u', 'p', {'brand': 'A', 'product': 'B', 'formula_id': 2}))
        cur.fetchall.return_value[0].update(formula_id=None, is_current=False)
        self.assertIsNotNone(diets._find_duplicate_history(cur, 'u', 'p', {'brand': 'A', 'product': 'B', 'formula_id': 2}))

    def test_unlinked_history_can_receive_verified_ids_without_overwriting_existing_dates(self):
        cur = MagicMock()
        row = {'id': 'h', 'status': 'active', 'is_current': False, 'started_at': 'known-date'}
        diets._merge_missing_history(cur, row, {'product_id': 10, 'formula_id': 20, 'started': 'different-date'})
        sql, args = cur.execute.call_args.args
        self.assertIn('product_id=%s', sql)
        self.assertIn('formula_id=%s', sql)
        self.assertNotIn('started_at', sql)
        self.assertEqual(args[-1], 'h')


class DailyRecordsTest(unittest.TestCase):
    def test_counts_and_amounts_remain_distinct_and_zero_is_a_value(self):
        row = daily.normalize_record({'day': '2026-09-20', 'food_count': 3, 'water_ml': 180, 'stool_count': 0})
        self.assertEqual(row['food_count'], 3)
        self.assertEqual(row['stool_count'], 0)
        self.assertNotIn('water_count', row)
        for value in (-1, 1.5, True, float('nan'), float('inf'), 1000, [], {}):
            with self.assertRaises(ValueError):
                daily.normalize_record({'day': '2026-09-20', 'food_count': value})

    def test_invalid_and_future_date_or_fake_complete_day_are_rejected(self):
        for value in ('2026-02-30', '2999-01-01', '20260920'):
            with self.assertRaises(ValueError):
                daily.normalize_record({'day': value})
        with self.assertRaises(ValueError):
            daily.normalize_record({'day': str(daily.today()), 'complete_day': True})

    def test_legacy_snapshots_are_sorted_validated_and_never_saved_or_complete(self):
        values = daily.normalize_client_records([{'day': '2026-09-20', 'water_ml': 180, 'complete_day': True},
                                                  {'day': '2026-09-21', 'food_count': 0}, {'day': 'invalid', 'food_count': 5}])
        self.assertEqual([r['day'] for r in values], ['2026-09-21', '2026-09-20'])
        self.assertFalse(values[1]['complete_day'])

    def test_chat_parser_never_saves_examples_plans_routines_or_multiple_dates(self):
        for value in ('如果今天进食3次', '比如今天进食3次', '每天进食3次', '今天进食3次？',
                      '今天进食3次，昨天饮水4次', '今天进食3次，进食4次', '进食3次'):
            self.assertIsNone(status.parse_counts(value))
        row = status.parse_counts('昨天全天进食3次，饮水5次，排便0次')
        self.assertEqual(row['stool_count'], 0)
        self.assertTrue(row['complete_day'])

    def test_partial_day_and_changed_source_cannot_generate_false_trend(self):
        pet = {'name': '测试猫', 'breed': '英短', 'sex': 'male'}
        rows = [{'day': str(daily.today() - timedelta(days=days)), 'food_count': value, 'complete_day': True, 'source': 'manual'}
                for days, value in ((3, 3), (2, 3), (1, 0))]
        result = status.build_status(pet, rows)
        self.assertEqual(result['trends'][0]['direction'], 'down')
        self.assertIn('英短、公', result['text'])
        rows[-1]['complete_day'] = False
        self.assertFalse(status.build_status(pet, rows)['trends'])
        rows[-1].update(complete_day=True, source='reported_device')
        self.assertFalse(status.build_status(pet, rows)['trends'])

    def test_old_client_records_are_not_current_state_evidence(self):
        result = status.build_status({}, [{'day': str(daily.today() - timedelta(days=30)), 'food_count': 0}])
        self.assertEqual(result['status'], 'needs_records')
        self.assertFalse(result['latest'])
        self.assertIn('超过最近14天', result['text'])

    def test_no_records_guides_all_three_counts_not_only_diet(self):
        result = status.build_status({}, [])
        self.assertEqual(set(result['missing_fields']), set(daily.COUNTS))
        self.assertIn('饮水次数', result['text'])
        self.assertIn('不生成所谓正常次数区间', result['text'])

    def test_status_entry_exits_old_food_flow_but_urgent_signs_win(self):
        state = chat._default_state('c')
        state.update(primary_intent='food_switch', current_step='food_health_state')
        decision = chat._recognize_guardian_turn('想聊聊我家宠物最近的状态，需要补充哪些信息？', state)
        self.assertTrue(decision[0]['_daily_status'])
        self.assertEqual(chat._recognize_guardian_turn('今天便血了，看看近期状态', state)[0]['_target_flow'], 'soft_stool')
        self.assertFalse(status.requested('分析一下现在吃的粮', state))

    def test_unbound_status_reply_keeps_conversation_turn_order(self):
        state = chat._default_state('c')
        decision = chat._recognize_guardian_turn('状态解读', state)
        result = chat._handle_context_message('u', {'pet_id': None}, '状态解读', state, intent_result=decision)
        self.assertEqual(result['result']['daily_status']['status'], 'pet_needed')
        self.assertEqual(result['result']['turn_no'], 1)
        self.assertEqual(result['result']['sequence_no'], 1)

    def test_save_is_owner_locked_absolute_upsert_with_per_metric_sources(self):
        conn, cur = MagicMock(), MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = {'data_json': {'day': '2026-09-20', 'water_count': 5, 'food_count': 2,
                                                   'metric_sources': {'water_count': 'manual'}}}
        with patch.object(daily, 'init_daily_record_table'), patch.object(daily, '_connect', return_value=conn), patch.object(daily, '_owned_pet') as owner:
            result = daily.save_daily_record('u', 'p', {'day': '2026-09-20', 'food_count': 3, 'source': 'reported_device'})
        owner.assert_called_once_with(cur, 'u', 'p', lock=True)
        self.assertEqual(result['record']['food_count'], 3)
        self.assertEqual(result['record']['water_count'], 5)
        self.assertEqual(result['record']['metric_sources']['water_count'], 'manual')
        self.assertEqual(result['record']['metric_sources']['food_count'], 'reported_device')
        self.assertIn('ON DUPLICATE KEY UPDATE', cur.execute.call_args.args[0])
        self.assertEqual(cur.execute.call_args.args[1][:3], ('u', 'p', '2026-09-20'))

    def test_api_requires_auth_ignores_payload_owner_and_uses_path_day(self):
        import api.miniprogram_api as api
        app = Flask(__name__); app.register_blueprint(miniprogram_api)
        client = app.test_client()
        self.assertEqual(client.get('/api/miniprogram/cat-profiles/p/daily-records').status_code, 401)
        with patch.object(api, '_current_user_id', return_value='actual-user'), patch.object(daily, 'save_daily_record', return_value={'ok': True}) as save:
            response = client.put('/api/miniprogram/cat-profiles/p/daily-records/2026-09-20', json={'user_id': 'other', 'day': '2026-09-21', 'food_count': 3})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(save.call_args.args[0], 'actual-user')
        self.assertEqual(save.call_args.args[2]['day'], '2026-09-20')


if __name__ == '__main__':
    unittest.main()
