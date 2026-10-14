"""Requirement 16/17 acceptance cases, using no external DB or model."""
from copy import deepcopy
import json
import unittest
from unittest.mock import MagicMock, patch

from services import guardian_food_scenario as rules
from services import guardian_product_portrait_service as portraits
from services import guardian_recommendation_service as rec
from services import guardian_food_flow_service as food
from services import miniprogram_chat_service as chat
from services.product_portrait_summary_service import build_portrait


def features(fid=1, **changes):
    return {'id': fid, 'formula_id': fid, 'feature_version': 'soft_v1', 'protein_score': .30,
            'fat_score': .60, 'fiber_score': 2.5, 'carb_score': 2., 'prebiotic_score': 2., **changes}


def product(fid):
    return {'formula_id': fid, 'product_id': fid * 10, 'catalog_key': f'formula:{fid}',
            'label': f'测试牌粮{fid}', 'brand': '测试牌', 'product_name': f'粮{fid}',
            'ingredient_composition': '鸭肉，米', 'life_stage': 'adult', 'product_type': '干粮'}


class ScenarioRulesTest(unittest.TestCase):
    def test_recovery_takes_priority_over_weight_loss_without_losing_goal(self):
        scenario = rules.build_scenario({'food_health_state': 'soft_stool', 'food_health_stage': 'recovering', 'food_goal': 'weight_loss'})
        self.assertEqual(scenario['goal'], 'weight_loss')
        self.assertIn('carb_score', scenario['keep'])
        self.assertNotIn('carb_score', scenario['improve'])
        base = features()
        good = rules.assess_candidate(features(2, fat_score=.55, prebiotic_score=2.25), base, scenario)
        drastic = rules.assess_candidate(features(3, fat_score=.10, prebiotic_score=4.5), base, scenario)
        self.assertTrue(good['eligible'])
        self.assertFalse(drastic['eligible'])

    def test_high_similarity_does_not_excuse_failure_to_improve(self):
        scenario = rules.build_scenario({'food_health_state': 'black_chin', 'food_health_stage': 'active', 'food_goal': 'black_chin'})
        self.assertFalse(rules.assess_candidate(features(2), features(), scenario)['eligible'])
        self.assertTrue(rules.assess_candidate(features(2, fat_score=.4), features(), scenario)['eligible'])

    def test_invalid_missing_and_nonfinite_data_is_never_neutral(self):
        scenario = rules.build_scenario({'food_health_state': 'none', 'food_goal': 'weight_loss'})
        for value in (None, float('nan'), float('inf'), -1, 5, 'invalid'):
            self.assertFalse(rules.assess_candidate(features(protein_score=value), features(), scenario)['eligible'])

    def test_prebiotic_is_bounded_and_does_not_always_mean_higher_is_better(self):
        scenario = rules.build_scenario({'food_health_state': 'soft_stool', 'food_health_stage': 'active'})
        self.assertFalse(rules.assess_candidate(features(prebiotic_score=5), features(), scenario)['eligible'])

    def test_balanced_support_does_not_turn_unchanged_food_into_an_improvement(self):
        for slots in ({'food_health_state': 'soft_stool', 'food_health_stage': 'active'},
                      {'food_health_state': 'none', 'food_goal': 'weight_loss'}):
            scenario = rules.build_scenario(slots)
            self.assertFalse(rules.assess_candidate(features(2), features(), scenario)['eligible'])
            better = rules.assess_candidate(features(2, fat_score=.5), features(), scenario)
            self.assertTrue(better['eligible'])
            self.assertFalse(any('益生元' in r for r in better['improvement_reasons']))

    def test_extra_small_improvement_cannot_reduce_a_candidates_score(self):
        scenario = rules.build_scenario({'food_health_state': 'soft_stool', 'food_health_stage': 'recovering'})
        fat_only = rules.assess_candidate(features(2, fat_score=.51), features(), scenario)
        both = rules.assess_candidate(features(3, fat_score=.51, prebiotic_score=2.05), features(), scenario)
        self.assertTrue(fat_only['eligible'] and both['eligible'])
        self.assertGreater(both['score'], fat_only['score'])

    def test_recovered_soft_stool_does_not_impose_fat_reduction_on_weight_gain(self):
        scenario = rules.build_scenario({'food_health_state': 'soft_stool', 'food_health_stage': 'recovered', 'food_goal': 'weight_gain'})
        self.assertNotIn('fat_score', scenario['improve'])
        self.assertEqual(scenario['status'], 'limited')
        self.assertIn('fiber_score', scenario['keep'])

    def test_active_gi_history_does_not_repeat_stage_question_forever(self):
        for phase in ('active', 'recovering'):
            scenario = rules.build_scenario({'food_health_state': 'gi_history', 'food_health_stage': phase})
            self.assertEqual(scenario['status'], 'limited')
            self.assertIsNone(rules.scenario_question(scenario))

    def test_status_goals_negation_and_hypotheticals_remain_distinct(self):
        for text in ('希望改善软便', '预防呕吐', '如果发生软便怎么办？', '没有软便', '不知道是否已经恢复？'):
            self.assertEqual(rules.extract_health(text, {}), {})
        values = rules.extract_health('软便，正在恢复中，想减重', {})
        self.assertEqual((values['food_health_state'], values['food_health_stage']), ('soft_stool', 'recovering'))
        self.assertEqual(rules.extract_health('现在黑下巴', {})['food_health_stage'], 'active')
        self.assertEqual(rules.extract_health('肠胃炎病史，已经恢复了', {})['food_health_stage'], 'recovered')

    def test_ongoing_vomiting_blocks_even_if_other_symptom_is_recovering(self):
        values = rules.extract_health('软便恢复中，今天又吐了', {})
        self.assertEqual(rules.build_scenario(values)['status'], 'blocked')
        values = rules.extract_health('呕吐已经恢复但今天又吐了', {})
        self.assertEqual(rules.build_scenario(values)['status'], 'blocked')

    def test_bare_condition_asks_for_phase_and_uncertainty_does_not_supply_one(self):
        values = rules.extract_health('软便', {})
        self.assertEqual(rules.build_scenario(values)['status'], 'needs_stage')
        self.assertEqual(rules.extract_health('好像已经恢复了', {'food_health_state': 'soft_stool'}), {})
        self.assertEqual(rules.extract_health('以前吃测试牌粮1时软便', {'food_health_state': 'none'}), {})

    def test_unknown_is_not_healthy_or_recovered(self):
        self.assertEqual(rules.build_scenario({})['status'], 'needs_state')
        self.assertEqual(rules.build_scenario({'food_health_state': 'soft_stool'})['status'], 'needs_stage')
        self.assertEqual(rules.build_scenario({'food_health_state': 'soft_stool', 'food_health_stage': 'unknown'})['status'], 'limited')
        self.assertEqual(rules.build_scenario({'food_health_state': 'unknown'})['status'], 'limited')

    def test_specialty_goals_do_not_invent_energy_coat_or_palatability(self):
        for goal in ('weight_gain', 'coat_health', 'palatability'):
            scenario = rules.build_scenario({'food_health_state': 'none', 'food_goal': goal})
            self.assertTrue(scenario['notes'])
            self.assertNotIn('fat_score', scenario['improve'])
            self.assertEqual(scenario['status'], 'limited')


class PortraitTest(unittest.TestCase):
    def test_common_complement_and_shortcoming_are_grounded_in_both_portraits(self):
        a = build_portrait(features(fat_score=.85, prebiotic_score=.5))
        b = build_portrait(features(2, fat_score=.5, prebiotic_score=2, carb_score=4.5))
        result = portraits.compare_portraits(a, b)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(any(r['dimension'] == 'protein' for r in result['common']))
        self.assertTrue(any(r['dimension'] == 'fat' for r in result['complements']))
        self.assertTrue(any(r['dimension'] == 'carbohydrate' for r in result['shortcomings']))
        self.assertIn('不表示建议混喂', portraits.portrait_comparison_text(result))

    def test_missing_and_wrong_version_do_not_generate_relationships(self):
        a = build_portrait(features())
        for b in (None, {**a, 'portrait_version': 'future_v2'}):
            result = portraits.compare_portraits(a, b)
            self.assertEqual(result['status'], 'unavailable')
            self.assertFalse(result['complements'])
        b = build_portrait(features(2, fiber_score=None))
        result = portraits.compare_portraits(a, b)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('纤维支持', result['missing_dimensions'])

    def test_repository_is_read_only_and_checks_source_current_and_versions(self):
        p = build_portrait(features())
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [{'formula_id': 1, 'portrait_json': json.dumps(p),
                                     'source_feature_id': 1, 'source_data_hash': p['source_data_hash']}]
        with patch.object(portraits.pymysql, 'connect', return_value=conn):
            result = portraits.load_product_portraits([1])
        self.assertEqual(result[1]['formula_id'], 1)
        cur.execute.assert_called_once()
        sql, params = cur.execute.call_args.args
        self.assertTrue(sql.strip().startswith('SELECT'))
        self.assertIn('f.is_current=1', sql)
        self.assertIn('MAX(v.id)', sql)
        self.assertIn('p.fat_score <=> s.fat_score', sql)
        self.assertIn('BINARY s.feature_version=BINARY p.feature_version', sql)
        self.assertEqual(params[:2], ['soft_v1', 'five_dimension_v1'])
        conn.commit.assert_not_called()

    def test_corrupt_rows_and_unbuilt_table_are_unavailable(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [{'formula_id': 1, 'portrait_json': 'invalid'}]
        with patch.object(portraits.pymysql, 'connect', return_value=conn):
            self.assertEqual(portraits.load_product_portraits([1]), {})
        with patch.object(portraits.pymysql, 'connect', side_effect=portraits.pymysql.OperationalError(1146, 'missing')):
            self.assertEqual(portraits.load_product_portraits([1]), {})


class ScenarioRecommendationTest(unittest.TestCase):
    def setUp(self):
        self.options = [product(i) for i in (1, 2, 3, 4)]
        self.pet = {'animal_type': 'cat', 'food_formula_id': 1, 'age_months': None}
        self.slots = {'food_health_state': 'soft_stool', 'food_health_stage': 'recovering',
                      'food_goal': 'weight_loss', 'current_food': '测试牌粮1',
                      'food_tolerance': 'current_good', '_tolerated_food': '测试牌粮1'}
        self.rows = {1: features(), 2: features(2, fat_score=.55, prebiotic_score=2.25),
                     3: features(3, fat_score=.1, prebiotic_score=4),
                     4: features(4, fat_score=.56, prebiotic_score=2.25)}
        for p in (patch.object(rec, 'list_standardized_product_options', return_value={'items': self.options}),
                  patch.object(rec, 'load_five_features', side_effect=lambda ids: {i: self.rows[i] for i in ids if i in self.rows}),
                  patch.object(rec, 'load_nutrition', return_value={}), patch.object(rec, 'load_risks', return_value={}),
                  patch.object(rec, 'load_product_portraits', side_effect=lambda ids: {i: build_portrait(self.rows[i]) for i in ids if i in self.rows})):
            p.start(); self.addCleanup(p.stop)

    def test_filters_history_and_excessive_change_before_ranking(self):
        result = rec.recommend_guardian_foods(self.pet, self.slots, [{'formula_id': 4}])
        self.assertEqual([i['formula_id'] for i in result['items']], [2])
        self.assertTrue(result['items'][0]['keep_reasons'])
        self.assertTrue(result['items'][0]['analysis']['portrait_comparison']['common'])
        self.assertEqual(result['reference']['source'], 'user_confirmed_current')
        self.assertTrue(any('未按年龄筛选' in c for c in result['items'][0]['cautions']))

    def test_current_food_and_health_topic_counts_do_not_prove_tolerance(self):
        self.slots.pop('food_tolerance')
        result = rec.recommend_guardian_foods(self.pet, self.slots, [{'formula_id': 1, 'health_topics': ['软便']}])
        self.assertEqual(result['status'], 'baseline_needed')
        self.assertEqual(result['items'], [])

    def test_real_history_feedback_can_supply_reference(self):
        self.slots.pop('food_tolerance')
        result = rec.recommend_guardian_foods(self.pet, self.slots, [{'formula_id': 1, 'note': '吃得适应，便便正常'}])
        self.assertEqual(result['reference']['source'], 'confirmed_history_feedback')
        self.assertEqual(result['status'], 'ready')

    def test_missing_data_and_zero_matches_do_not_fall_back_to_old_similarity(self):
        self.rows[1]['fat_score'] = None
        self.assertEqual(rec.recommend_guardian_foods(self.pet, self.slots)['status'], 'baseline_features_missing')
        self.rows[1] = features()
        self.rows[2]['protein_score'] = .9
        self.rows[4]['protein_score'] = None
        self.assertEqual(rec.recommend_guardian_foods(self.pet, self.slots)['status'], 'no_scenario_match')

    def test_changed_current_food_invalidates_tolerance_confirmation(self):
        self.slots['current_food'] = '新粮'
        self.assertEqual(rec.recommend_guardian_foods(self.pet, self.slots)['status'], 'baseline_needed')

    def test_unsupported_specialty_goal_never_ranks_arbitrary_products(self):
        self.slots.update(food_health_state='none', food_goal='palatability')
        result = rec.recommend_guardian_foods(self.pet, self.slots)
        self.assertEqual(result['status'], 'limited')
        self.assertEqual(result['items'], [])


class ChatAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.state = chat._default_state('c')
        self.context = {'pet_profile': {'animal_type': 'cat', 'food_formula_id': 1},
                        'current_food': {'brand': '测试牌', 'product_name': '粮1'}, 'diet_history': []}
        for p in (patch.object(chat, '_build_context', return_value=self.context),
                  patch.object(chat, '_generate_answer', return_value=('根据现有数据比较如下。', None)),
                  patch.object(chat, '_prepare_food_handoff'),
                  patch.object(food, 'list_standardized_product_options', return_value={'items': [product(1), product(2)]}),
                  patch.object(food, 'load_nutrition', return_value={}), patch.object(food, 'load_risks', return_value={}),
                  patch.object(food, 'load_product_portraits', return_value={1: build_portrait(features()), 2: build_portrait(features(2, fat_score=.4))})):
            p.start(); self.addCleanup(p.stop)

    def turn(self, text='', interaction=None):
        if interaction:
            self.state['primary_intent'] = 'food_switch'
        decision = chat._recognize_guardian_turn(text, self.state) if text else None
        return chat._handle_context_message('u', {'pet_id': 'p'}, text, self.state,
            interaction=interaction, forced_intent='food_switch', intent_result=decision)

    def test_missing_state_then_phase_asks_only_missing_and_preserves_goal(self):
        self.assertEqual(self.turn('帮我选一款减重粮')['interaction']['slot'], 'food_health_state')
        self.assertEqual(self.turn(interaction={'slot': 'food_health_state', 'value': 'soft_stool'})['interaction']['slot'], 'food_health_stage')
        with patch.object(chat, 'recommend_guardian_foods', return_value={'status': 'baseline_needed', 'items': [], 'note': '需要耐受参考粮'}) as recommend:
            result = self.turn('恢复中')
        self.assertEqual(recommend.call_args.args[1]['food_health_stage'], 'recovering')
        self.assertEqual(recommend.call_args.args[1]['food_goal'], 'weight_loss')
        self.assertEqual(result['interaction']['slot'], 'food_tolerance')

    def test_combined_request_preserves_state_phase_and_long_term_goal(self):
        with patch.object(chat, 'recommend_guardian_foods', return_value={'status': 'baseline_needed', 'items': [], 'note': '需要耐受参考粮'}):
            self.turn('软便恢复中，想减重，帮我推荐一款粮')
        self.assertEqual(self.state['slots']['food_goal'], 'weight_loss')
        self.assertEqual(self.state['slots']['food_health_stage'], 'recovering')
        with patch.object(chat, 'recommend_guardian_foods', return_value={'status': 'baseline_needed', 'items': [], 'note': '需要耐受参考粮'}):
            self.turn('软便已经恢复了')
        self.assertEqual(self.state['slots']['food_health_stage'], 'recovered')
        self.assertEqual(self.state['slots']['food_goal'], 'weight_loss')

    def test_limited_data_reply_is_not_replaced_by_generic_model_advice(self):
        with patch.object(chat, '_generate_answer') as model:
            self.turn('帮我选粮')
            result = self.turn(interaction={'slot': 'food_health_state', 'value': 'unknown'})
            # Goal can still be clarified; supply it if needed.
            if self.state['current_step'] == 'food_goal':
                result = self.turn(interaction={'slot': 'food_goal', 'value': 'general'})
        model.assert_not_called()
        self.assertIn('身体状态尚未确认', result['reply'])

    def test_target_comparison_reads_portrait_without_updating_actual_food(self):
        self.state['slots'].update(_food_mode='selection', current_food='测试牌粮1')
        with patch.object(food, 'update_cat_profile') as update:
            result = self.turn(interaction={'slot': 'target_food', 'value': 'formula:2'})
        self.assertIn('共同特点', result['reply'])
        self.assertIn('潜在短板', result['reply'])
        self.assertEqual(result['result']['food_analysis']['product_portrait']['formula_id'], 2)
        update.assert_not_called()

    def test_urgent_risk_still_wins_and_invalid_selections_cannot_forge_state(self):
        self.assertEqual(chat._recognize_guardian_turn('便血了，帮我选粮', self.state)[0]['_target_flow'], 'soft_stool')
        for interaction in ({'slot': 'food_health_stage', 'value': 'made_up'}, {'slot': '_food_scenario', 'value': {}}):
            with self.assertRaises(ValueError):
                self.turn(interaction=interaction)

    def test_new_phase_invalidates_unstarted_target_and_plan(self):
        self.state['slots'].update(food_health_state='soft_stool', food_health_stage='recovering',
                                   food_goal='weight_loss', target_food='测试牌粮2',
                                   _food_flow={'stage': 'planned', 'target': product(2), 'candidates': [product(2)]})
        with patch.object(chat, 'recommend_guardian_foods', return_value={'status': 'no_scenario_match', 'items': [], 'note': '重新核验'}) as recommend:
            self.turn('现在又软便了')
        recommend.assert_called_once()
        self.assertNotIn('target_food', self.state['slots'])
        self.assertFalse(self.state['slots']['_food_flow'].get('target'))

    def test_recovered_vomiting_does_not_route_as_current_but_relapse_does(self):
        self.assertEqual(chat._recognize_guardian_turn('呕吐已恢复，帮我选粮', self.state)[0]['_target_flow'], 'food_switch')
        self.assertEqual(chat._recognize_guardian_turn('呕吐已恢复但今天又吐了，帮我选粮', self.state)[0]['_target_flow'], 'soft_stool')


if __name__ == '__main__':
    unittest.main()
