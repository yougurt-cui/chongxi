import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from flask import Flask
from api.miniprogram_api import miniprogram_api
from services import miniprogram_food_submission_service as uploads
from services import miniprogram_diet_history_service as diets
from services import miniprogram_chat_service as chat
from services import guardian_recommendation_service as recommendations


class RecognitionTest(unittest.TestCase):
    def test_source_id_does_not_establish_success(self):
        for output in ({'source_id': 1}, {'source_id': 1, 'ingredient_composition': '  '}):
            self.assertEqual(uploads._recognition_status({'task_status': 'done'}, output), 'unrecognized')

    def test_ocr_node_state_is_independent_of_later_pipeline_failure(self):
        output = {'source_id': 1, 'ingredient_composition': '鸡肉，米，鱼油'}
        for task_status in ('processing', 'done', 'failed'):
            task = {'task_status': task_status, 'nodes': [{'node_code': 'ocr_formula', 'node_status': 'success'}]}
            self.assertEqual(uploads._recognition_status(task, output), 'success')
        self.assertEqual(uploads._recognition_status({'nodes': [{'node_code': 'ocr_formula', 'node_status': 'need_review'}]}, output), 'needs_review')

    def test_partial_ocr_output_keeps_polling_until_node_finishes(self):
        for status in ('running', 'waiting_result', 'ready'):
            self.assertEqual(uploads._recognition_status({'nodes': [{'node_code': 'ocr_formula', 'node_status': status}]}, {'source_id': 1}), 'processing')

    def test_failed_upstream_or_missing_task_does_not_poll_forever(self):
        self.assertEqual(uploads._recognition_status({'task_status': 'failed', 'nodes': [{'node_code': 'ocr_formula', 'node_status': 'pending'}]}, {}), 'failed')
        with patch.object(uploads, 'get_orchestrator_task', return_value=None):
            row = uploads._sync_recognition({'id': 'f1', 'status': 'active', 'recognition_status': 'processing', 'orchestrator_task_id': 'missing'})
        self.assertEqual(row['recognition_status'], 'failed')

    def test_terminal_empty_results_stop_polling(self):
        for task in ({'task_status': 'done'}, {'task_status': 'need_review'}, {'nodes': [{'node_code': 'ocr_formula', 'node_status': 'need_review'}]}):
            self.assertEqual(uploads._recognition_status(task, {}), 'unrecognized')
        self.assertEqual(uploads._recognition_status({'task_status': 'failed'}, {}), 'failed')
        self.assertEqual(uploads._recognition_status({'task_status': 'processing'}, {}), 'processing')

    def test_old_false_success_is_guarded_at_serialization(self):
        item = uploads._serialize({'recognition_status': 'success', 'source_id': 1, 'status': 'active'})
        self.assertEqual(item['recognition_status'], 'unrecognized')
        self.assertFalse(item['can_analyze'])
        self.assertTrue(item['can_reupload'])
        self.assertIn('背面', item['recognition_message'])

    def test_cancelled_record_cannot_be_analyzed_or_reuploaded(self):
        item = uploads._serialize({'recognition_status': 'success', 'ingredient_composition': '鸡肉', 'status': 'cancelled'})
        self.assertFalse(item['can_analyze'])
        self.assertFalse(item['can_reupload'])

    def test_chat_does_not_generate_answer_for_unrecognized_submission(self):
        state = chat._default_state('c1')
        submission = {'id': 'food1', 'recognition_status': 'unrecognized', 'recognition_message': '请补拍背面的完整配料表'}
        with patch.object(chat, '_build_context', return_value={'food_submission': submission}), patch.object(chat, '_generate_answer') as model:
            result = chat._handle_context_message('u1', {'pet_id': 'p1'}, '', state, attachments=[{'type': 'food_submission', 'submission_id': 'food1'}])
        model.assert_not_called()
        self.assertEqual(result['reply'], submission['recognition_message'])


class DietHistoryTest(unittest.TestCase):
    def test_dates_are_in_shanghai_and_end_date_is_inclusive(self):
        data = diets.normalize_history({'brand': '甲', 'product': 'A', 'started_on': '2026-09-01', 'ended_on': '2026-09-02'})
        self.assertEqual(data['started'], datetime(2026, 8, 31, 16))
        self.assertEqual(data['ended'], datetime(2026, 9, 2, 16))
        for dates in ({'started_on': '2026-09-02', 'ended_on': '2026-09-01'}, {'started_on': 'invalid'}, {'ended_on': '2999-01-01'}):
            with self.assertRaises(ValueError):
                diets.normalize_history({'brand': '甲', 'product': 'A', **dates})

    def test_history_without_dates_remains_unknown(self):
        data = diets.normalize_history({'brand': '甲', 'product': 'A'})
        self.assertIsNone(data['started'])
        self.assertIsNone(data['ended'])

    def test_food_change_and_history_share_profile_transaction(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {'id': 'old1'}
        now = datetime(2026, 10, 1, 4)
        diets.record_profile_diet(cursor, 'u1', 'p1', {'food_brand': '甲', 'food_product': 'A'}, {'food_brand': '乙', 'food_product': 'B'}, now)
        statements = cursor.execute.call_args_list
        self.assertIn('FOR UPDATE', statements[0].args[0])
        self.assertIn('ended_at=%s,is_current=0', statements[1].args[0])
        self.assertEqual(statements[1].args[1], (now, 'old1'))
        self.assertIn('INSERT INTO', statements[2].args[0])
        self.assertIn('profile_confirmed', statements[2].args[1])
        self.assertIn('p1', statements[2].args[1])

    def test_unchanged_food_does_not_append_history(self):
        cursor = MagicMock(); cursor.fetchone.return_value = {'id': 'current'}
        pet = {'food_brand': '甲', 'food_product': 'A'}
        diets.record_profile_diet(cursor, 'u1', 'p1', pet, pet)
        self.assertEqual(cursor.execute.call_count, 1)

    def test_legacy_snapshot_never_invents_a_start_date(self):
        cursor = MagicMock(); cursor.fetchone.return_value = None
        with patch.object(diets, '_insert') as insert:
            diets.record_profile_diet(cursor, 'u1', 'p1', {'food_brand': '甲', 'food_product': 'A'}, {'food_brand': '乙', 'food_product': 'B'})
        first = insert.call_args_list[0]
        self.assertEqual(first.kwargs['source'], 'profile_snapshot')
        self.assertNotIn('started', first.kwargs)
        self.assertFalse(first.kwargs['current'])

    def test_history_food_key_ignores_display_spacing_and_punctuation(self):
        self.assertEqual(diets._food_key(' 鲜朗 ', '烘焙-全价 猫粮'), ('鲜朗', '烘焙全价猫粮'))

    def test_import_reuses_matching_profile_history(self):
        conn, cursor = MagicMock(), MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [{'id': 'pet-row'}, None]
        cursor.fetchall.return_value = [{
            'id': 'profile-history', 'brand': '鲜朗', 'product': '烘焙全价猫粮',
            'source': 'profile_confirmed', 'status': 'active', 'is_current': 0,
            'started_at': None, 'ended_at': datetime(2026, 10, 13), 'note': '',
        }]
        with patch.object(diets, 'init_diet_history_table'), patch.object(diets, '_connect', return_value=conn), patch.object(diets, '_insert') as insert:
            result = diets.add_diet_history('u1', 'p1', {
                'brand': '鲜朗', 'product': '烘焙全价猫粮',
                'note': '本机旧记录，吃粮日期待确认', 'legacy_record_id': 'local-1',
            })
        insert.assert_not_called()
        self.assertEqual(result, {'ok': True, 'id': 'profile-history', 'deduplicated': True})
        self.assertTrue(any('UPDATE miniprogram_pet_diet_history SET note=%s' in call.args[0]
                            for call in cursor.execute.call_args_list))

    def test_different_history_food_still_inserts(self):
        conn, cursor = MagicMock(), MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = {'id': 'pet-row'}
        cursor.fetchall.return_value = []
        with patch.object(diets, 'init_diet_history_table'), patch.object(diets, '_connect', return_value=conn), patch.object(diets, '_insert', return_value='new-id') as insert:
            result = diets.add_diet_history('u1', 'p1', {'brand': '皇家', 'product': 'EP42'})
        insert.assert_called_once()
        self.assertEqual(result, {'ok': True, 'id': 'new-id', 'deduplicated': False})

    def test_consultation_frequency_counts_conversations_not_short_answers(self):
        messages = [
            {'id': 'm1', 'conversation_id': 'c1', 'content': '最近软便怎么照顾', 'created_at': datetime(2026, 9, 1)},
            {'id': 'm2', 'conversation_id': 'c1', 'content': '软便的选粮方向', 'created_at': datetime(2026, 9, 2)},
            {'id': 'm3', 'conversation_id': 'c2', 'content': '最近掉毛', 'created_at': datetime(2026, 9, 3)},
        ]
        topics = {t['code']: t for t in diets.health_associations(messages)}
        self.assertEqual(topics['digestive']['conversation_count'], 1)
        self.assertEqual(topics['skin']['conversation_count'], 1)
        self.assertEqual(topics['digestive']['samples'][0]['conversation_id'], 'c1')

    def test_history_api_uses_bearer_identity_not_payload_owner(self):
        app = Flask(__name__); app.register_blueprint(miniprogram_api)
        with patch('api.miniprogram_api._current_user_id', return_value='owner'), patch('api.miniprogram_api.add_diet_history', return_value={'ok': True, 'id': 'h1'}) as add:
            response = app.test_client().post('/api/miniprogram/cat-profiles/p1/diet-history', json={'user_id': 'other', 'brand': '甲', 'product': 'A'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(add.call_args.args[:2], ('owner', 'p1'))
        self.assertEqual(app.test_client().get('/api/miniprogram/cat-profiles/p1/diet-history').status_code, 401)


class RecommendationTest(unittest.TestCase):
    def setUp(self):
        mock = patch.object(recommendations, 'load_nutrition', return_value={})
        mock.start(); self.addCleanup(mock.stop)
        risk_mock = patch.object(recommendations, 'load_risks', return_value={})
        risk_mock.start(); self.addCleanup(risk_mock.stop)
        self.pet = {'animal_type': 'cat', 'age_months': 24, 'allergies': []}
        self.food = {'formula_id': 1, 'product_id': 2, 'catalog_key': 'formula:1', 'brand': '乙', 'product_name': 'B',
                     'life_stage': 'adult', 'product_type': '干粮', 'ingredient_composition': '鸡肉，米', 'price': 60, 'price_max_per_jin': 70, 'origin_type': '国产品牌'}

    def test_budget_must_have_a_per_jin_unit(self):
        self.assertEqual(recommendations.explicit_preferences('预算100元一袋'), {})
        self.assertEqual(recommendations.explicit_preferences('每斤50元，国产'), {'budget_per_jin': 50, 'origin_type': 'domestic'})
        self.assertEqual(recommendations.explicit_preferences('80元/斤以内'), {'budget_per_jin': 80})

    def test_suitability_budget_and_allergy_filters(self):
        self.assertTrue(recommendations.eligible_food(self.food, self.pet, {}))
        self.assertFalse(recommendations.eligible_food(self.food, {**self.pet, 'age_months': 6}, {}))
        self.assertFalse(recommendations.eligible_food(self.food, {**self.pet, 'allergies': ['鸡肉']}, {}))
        self.assertFalse(recommendations.eligible_food(self.food, {**self.pet, 'allergies': ['未知过敏原']}, {}))
        self.assertFalse(recommendations.eligible_food(self.food, self.pet, {'budget_per_jin': 50}))
        self.assertTrue(recommendations.eligible_food(self.food, self.pet, {'origin_type': 'domestic'}))
        self.assertFalse(recommendations.eligible_food({**self.food, 'life_stage': None}, self.pet, {}))
        self.assertFalse(recommendations.eligible_food({**self.food, 'product_type': '零食'}, self.pet, {}))

    def test_unsupported_or_medical_cases_do_not_run_engine(self):
        self.assertEqual(recommendations.recommend_guardian_foods({'animal_type': 'dog'}, {})['items'], [])
        self.assertEqual(recommendations.recommend_guardian_foods({**self.pet, 'diseases': ['肾病']}, {})['status'], 'medical_diet_required')

    def test_missing_age_does_not_block_known_stage_candidates(self):
        pet = {**self.pet, 'age_months': None}
        for stage in ('kitten', 'adult', 'senior', 'all_stages'):
            self.assertTrue(recommendations.eligible_food({**self.food, 'life_stage': stage}, pet, {}))
        self.assertTrue(recommendations.eligible_food(self.food, {'animal_type': 'cat'}, {}))
        self.assertFalse(recommendations.eligible_food({**self.food, 'life_stage': None}, pet, {}))
        self.assertFalse(recommendations.eligible_food(self.food, pet, {'budget_per_jin': 50}))
        self.assertFalse(recommendations.eligible_food(self.food, {**pet, 'allergies': ['鸡肉']}, {}))

    def test_missing_age_returns_ranked_candidates_without_inventing_age(self):
        import pandas as pd
        from consumer_apps import product_recommendation_engine as engine
        pool = pd.DataFrame([{'formula_id': 1, 'product_name': 'B', 'brand': '乙', 'protein_quality_score': 80}])
        profiles = {'digest': {'profile_code': 'digest', 'symptom_type': 'soft_stool', 'weights': {'protein_quality': 1}}}
        pet = {**self.pet, 'age_months': None}
        with patch('sqlalchemy.create_engine', return_value=MagicMock()), patch.object(engine, 'load_recommendation_profiles', return_value=profiles), patch.object(engine, 'load_product_pool', return_value=pool), patch.object(recommendations, 'list_standardized_product_options', return_value={'items': [self.food]}):
            result = recommendations.recommend_guardian_foods(pet, {'switch_reason': 'digestive'})
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['items'][0]['formula_id'], 1)
        self.assertEqual(result['items'][0]['fit_score'], 80)
        self.assertIn('未按年龄筛选', result['items'][0]['cautions'][-1])
        self.assertIn('成猫', result['items'][0]['cautions'][-1])
        self.assertIsNone(pet['age_months'])

    def test_only_a_confirmed_profile_change_refreshes_soft_stool_baseline(self):
        from services import soft_stool_assistant_service as soft
        state = {'pet_id': 'p1', 'turn': 1, 'context': {'profile_diet_signature': ['甲', 'A', 1], 'baseline_loaded': True, 'diet_override': {'baseline_food': 'A'}},
                 'slots': {'current_product': 'A'}, 'analysis': {'diet_compare': {'old': 'A'}}, 'slot_status': {'current_product': {'status': 'confirmed'}}}
        def run(**kwargs):
            self.assertFalse(kwargs['state']['context']['baseline_loaded'])
            self.assertNotIn('current_product', kwargs['state']['slots'])
            self.assertNotIn('diet_override', kwargs['state']['context'])
            return {'state': kwargs['state']}
        with patch.object(soft, 'get_cat_profile', return_value={'food_brand': '乙', 'food_product': 'B', 'food_formula_id': 2}), patch.object(soft, 'run_soft_stool_turn', side_effect=run):
            result = soft.handle_turn('u1', {'pet_id': 'p1', 'message': '继续聊软便', 'state': state})
        self.assertEqual(result['state']['context']['profile_diet_signature'], ['乙', 'B', 2])
        self.assertTrue(state['context']['baseline_loaded'])

    def test_engine_ranks_only_real_current_eligible_candidates(self):
        import pandas as pd
        from consumer_apps import product_recommendation_engine as engine
        pool = pd.DataFrame([{'formula_id': 1, 'product_name': 'B', 'brand': '乙', 'protein_quality_score': 80},
                             {'formula_id': 9, 'product_name': 'unknown', 'brand': '未知', 'protein_quality_score': 100}])
        profiles = {'digest': {'profile_code': 'digest', 'profile_name': '肠胃支持', 'symptom_type': 'soft_stool', 'weights': {'protein_quality': 1}}}
        with patch('sqlalchemy.create_engine', return_value=MagicMock()), patch.object(engine, 'load_recommendation_profiles', return_value=profiles), patch.object(engine, 'load_product_pool', return_value=pool), patch.object(recommendations, 'list_standardized_product_options', return_value={'items': [self.food]}):
            result = recommendations.recommend_guardian_foods(self.pet, {'switch_reason': 'digestive'})
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['formula_id'], 1)
        self.assertEqual(result['items'][0]['fit_score'], 80)

    def test_missing_metrics_never_receive_neutral_default_scores(self):
        import pandas as pd
        from consumer_apps import product_recommendation_engine as engine
        pool = pd.DataFrame([{'formula_id': 1, 'product_name': 'B', 'brand': '乙', 'protein_quality_score': None}])
        profiles = {'digest': {'profile_code': 'digest', 'symptom_type': 'soft_stool', 'weights': {'protein_quality': 1}}}
        with patch('sqlalchemy.create_engine', return_value=MagicMock()), patch.object(engine, 'load_recommendation_profiles', return_value=profiles), patch.object(engine, 'load_product_pool', return_value=pool), patch.object(recommendations, 'list_standardized_product_options', return_value={'items': [self.food]}):
            result = recommendations.recommend_guardian_foods(self.pet, {'switch_reason': 'digestive'})
        self.assertEqual(result['items'], [])
        self.assertEqual(result['status'], 'data_unavailable')


if __name__ == '__main__':
    unittest.main()
