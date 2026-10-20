"""Product analysis: evidence, presentation groups, scopes and usage choice."""
from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch

from services import guardian_health_analysis_service as health
from services import guardian_health_flow_service as flow
from services import miniprogram_chat_service as chat
from services.product_portrait_summary_service import build_portrait


def portrait(fid=1, **values):
    return build_portrait({'id': fid, 'formula_id': fid, 'feature_version': 'soft_v1',
        'protein_quality_score': .9, 'protein_score': .2, 'fat_score': .2,
        'fiber_score': .5, 'carb_score': 1, 'prebiotic_score': .5, **values})


def mapping(code, level, focus, index=1, **changes):
    return {'mapping_code': f'{code}-{level}-{index}', 'factor_code': code, 'factor_status': level,
            'health_focus': focus, 'health_mechanism': f'{code}的可核验机制{index}。',
            'user_observation': f'观察{focus}。', 'knowledge_version': 'v1', 'review_status': 'pending',
            'status': 'active', 'relation_type': None, **changes}


def knowledge():
    return {'status': 'ready', 'rows': [mapping('FIBER_SUPPORT', 'low', '软便'),
        mapping('PREBIOTIC_SUPPORT', 'low', '便便稳定'), mapping('PROTEIN_QUALITY', 'high', '肌肉维持'),
        mapping('PROTEIN_PRESSURE', 'low', '消化耐受'), mapping('FAT_BURDEN', 'low', '消化耐受'),
        mapping('CARB_BURDEN', 'low', '消化耐受')]}


def analysis(fid=1):
    return {'product': {'formula_id': fid, 'brand': '测试牌', 'product_name': f'粮{fid}'},
            'product_portrait': portrait(fid)}


class HealthEvidenceTest(unittest.TestCase):
    def test_groups_six_dimensions_into_traceable_presentation_sections(self):
        result = health.analyse_portrait(portrait(), knowledge())
        self.assertEqual(result['status'], 'ready')
        groups = {(r['title'], r['kind']): r for r in result['concerns']}
        self.assertEqual(len(groups[('便便状态', 'attention')]['evidence']), 2)
        self.assertEqual(len(groups[('消化耐受', 'advantage')]['evidence']), 3)
        self.assertEqual(groups[('肌肉与体态维持', 'advantage')]['tag'], '配方优势参考')
        self.assertTrue(all(s['knowledge_version'] == 'v1' for g in groups.values() for s in g['sources']))
        self.assertIn('可核验机制', groups[('肌肉与体态维持', 'advantage')]['mechanism_text'])
        self.assertNotIn('可核验机制', health.report_text('粮1', result))
        self.assertEqual(result['presentation']['advantages'][0]['title'], '肌肉维持')
        self.assertEqual(result['presentation']['attentions'][0]['title'], '排便稳定')
        self.assertNotIn('/5', health.report_text('粮1', result))

    def test_favourable_and_unfavourable_signals_for_one_topic_are_split(self):
        rows = [
            mapping('FIBER_SUPPORT', 'high', '便便稳定', relation_type='attention'),
            mapping('PREBIOTIC_SUPPORT', 'low', '便便稳定', relation_type='attention'),
        ]
        result = health.analyse_portrait(
            portrait(fiber_score=4.12, prebiotic_score=1.16),
            {'status': 'ready', 'rows': rows},
        )
        groups = {(item['title'], item['kind']) for item in result['concerns']}
        self.assertEqual(groups, {('便便状态', 'advantage'), ('便便状态', 'attention')})
        self.assertEqual(result['presentation']['advantages'][0]['title'], '排便健康')
        self.assertEqual(result['presentation']['attentions'][0]['title'], '排便稳定')
        self.assertIn('纤维支持较好', result['presentation']['advantages'][0]['summary'])
        self.assertIn('益生元支持偏少', result['presentation']['attentions'][0]['summary'])
        self.assertNotIn('不保证', health.report_text('粮1', result))

    def test_same_factor_uses_distinct_copy_for_digestive_and_stool_topics(self):
        rows = [
            mapping('PREBIOTIC_SUPPORT', 'low', '胀气'),
            mapping('PREBIOTIC_SUPPORT', 'low', '便便稳定'),
        ]
        result = health.analyse_portrait(
            portrait(prebiotic_score=1.16), {'status': 'ready', 'rows': rows}
        )
        items = {item['title']: item['summary'] for item in result['presentation']['attentions']}
        self.assertIn('腹胀或排气增多', items['软便、胀气'])
        self.assertIn('忽软忽硬', items['排便稳定'])

    def test_all_intermediate_levels_skip_high_and_low_mechanisms(self):
        for values in ({'protein_quality_score': .5, 'protein_score': .5, 'fat_score': .5,
                        'fiber_score': 2, 'carb_score': 2, 'prebiotic_score': 2},
                       {'protein_quality_score': .75, 'protein_score': .75, 'fat_score': .75,
                        'fiber_score': 3.75, 'carb_score': 3.75, 'prebiotic_score': 3.75}):
            report = health.analyse_portrait(portrait(**values), knowledge())
            self.assertEqual(report['status'], 'no_match')
            self.assertEqual(report['concerns'], [])
            self.assertEqual(len(report['skipped_dimensions']), 6)
            self.assertIn('不能据此判断没有健康风险', report['message'])

    def test_missing_nonfinite_and_out_of_range_are_not_low_scores(self):
        for value in (None, float('nan'), float('inf'), -1, 9):
            p = portrait()
            p['dimensions']['fiber']['score'] = value
            result = health.analyse_portrait(p, knowledge())
            self.assertIn('纤维支持', result['missing_dimensions'])
            self.assertFalse(any(e['dimension'] == 'fiber' for g in result['concerns'] for e in g['evidence']))

    def test_wrong_versions_disabled_rejected_and_empty_rows_never_match(self):
        for changes in ({'status': 'inactive'}, {'review_status': 'rejected'}, {'knowledge_version': 'v2'},
                        {'health_mechanism': ''}, {'mapping_code': ''}):
            result = health.analyse_portrait(portrait(), {'status': 'ready', 'rows': [
                mapping('FIBER_SUPPORT', 'low', '软便', **changes)]})
            self.assertEqual(result['concerns'], [])
        result = health.analyse_portrait({**portrait(), 'portrait_version': 'old'}, knowledge())
        self.assertEqual(result['reason'], 'portrait_unavailable')

    def test_unavailable_knowledge_and_insufficient_matches_are_explicit(self):
        result = health.analyse_portrait(portrait(), {'status': 'unavailable'})
        self.assertEqual(result['reason'], 'knowledge_unavailable')
        result = health.analyse_portrait(portrait(), {'status': 'ready', 'rows': [knowledge()['rows'][0]]})
        self.assertEqual(len(result['concerns']), 1)
        self.assertEqual(result['status'], 'partial')

    def test_mapping_repository_is_read_only_pinned_and_handles_missing_table(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = knowledge()['rows']
        with patch.object(health.pymysql, 'connect', return_value=conn):
            self.assertEqual(health.load_health_mappings()['status'], 'ready')
        sql, params = cur.execute.call_args.args
        self.assertTrue(sql.strip().startswith('SELECT'))
        self.assertEqual(params, ('v1',))
        self.assertIn("status='active'", sql)
        conn.commit.assert_not_called()
        with patch.object(health.pymysql, 'connect', side_effect=health.pymysql.OperationalError(1146, 'missing')):
            self.assertEqual(health.load_health_mappings()['status'], 'unavailable')


class HealthChatTest(unittest.TestCase):
    def setUp(self):
        self.state = chat._default_state('c1')
        self.conversation = {'id': 'c1', 'pet_id': 'pet1'}
        self.context = {'pet_profile': {'animal_type': 'cat', 'food_formula_id': 1},
                        'current_food': {'formula_id': 1, 'brand': '测试牌', 'product_name': '粮1'},
                        'diet_history': [{'formula_id': 2, 'brand': '测试牌', 'product': '粮2'}]}
        for mock in (patch.object(chat, '_build_context', side_effect=lambda *a: deepcopy(self.context)),
                     patch.object(chat, '_food_analysis_by_formula', side_effect=lambda fid, *a: analysis(fid) if fid else None),
                     patch.object(flow, 'load_health_mappings', side_effect=knowledge),
                     patch.object(chat, 'get_chat_model_config', return_value={'api_key': ''}),
                     patch.object(chat, '_generate_answer', return_value=('已有推荐说明', None))):
            mock.start(); self.addCleanup(mock.stop)

    def turn(self, message='', interaction=None, **kwargs):
        decision = chat._recognize_guardian_turn(message, self.state) if message and not interaction else None
        return chat._handle_context_message('u1', self.conversation, message, self.state,
            interaction=interaction, intent_result=decision, **kwargs)

    def analyse(self):
        return self.turn('帮我分析一下当前粮')

    def confirm(self, reply, value='current', **changes):
        return self.turn(interaction={'slot': flow.CONFIRM_SLOT, 'value': value,
            'analysis_id': reply['result']['analysis_id'], **changes})

    def test_current_food_analysis_has_structured_sections_and_never_recalls(self):
        with patch.object(chat, 'recommend_guardian_foods') as recall:
            result = self.analyse()
        recall.assert_not_called()
        self.assertEqual(result['result']['food_candidates'], [])
        subject = result['result']['health_analysis']['subjects'][0]
        self.assertTrue(subject['presentation']['advantages'])
        self.assertTrue(subject['presentation']['attentions'])
        self.assertEqual(self.state['current_step'], flow.CONFIRM_SLOT)

    def test_history_and_current_comparison_keeps_both_sides_and_diet_facts(self):
        result = self.turn('帮我对比一下当前粮和历史粮')
        data = result['result']['health_analysis']
        self.assertEqual([r['formula_id'] for r in data['subjects']], [1, 2])
        self.assertEqual(len(data['comparisons']), 1)
        self.assertIn('粮1', data['comparisons'][0]['left'])
        self.assertIn('粮2', data['comparisons'][0]['right'])
        self.assertNotIn('target_food', self.state['slots'])

    def test_upload_resolves_to_analysis_without_claiming_it_is_current(self):
        self.context['food_submission'] = {'id': 'upload1', 'formula_id': 3, 'recognition_status': 'success',
            'ingredient_composition': '鸭肉，米', 'recognized_brand': '上传牌', 'recognized_product_name': '上传粮'}
        result = self.turn(attachments=[{'type': 'food_submission', 'submission_id': 'upload1'}])
        self.assertEqual(result['result']['health_analysis']['subjects'][0]['formula_id'], 3)
        self.assertEqual(self.context['current_food']['formula_id'], 1)
        self.assertNotEqual(self.state['slots'].get('current_food'), '上传牌 上传粮')

    def test_considering_preserves_analysis_and_cannot_be_replayed(self):
        original = self.analyse()
        with patch.object(chat, 'recommend_guardian_foods') as recall:
            result = self.confirm(original, 'considering')
            with self.assertRaises(ValueError):
                self.confirm(original)
        recall.assert_not_called()
        self.assertEqual(result['result']['analysis_stage'], 'considering')
        self.assertTrue(self.state['slots'][flow.STATE_KEY]['subjects'])

    def test_old_client_echoes_scoped_value_without_new_top_level_fields(self):
        original = self.analyse()
        option = original['interaction']['options'][1]
        result = self.turn(interaction={'slot': flow.CONFIRM_SLOT, 'value': option['value']})
        self.assertEqual(result['result']['analysis_stage'], 'considering')
        with self.assertRaises(ValueError):
            self.turn(interaction={'slot': flow.CONFIRM_SLOT, 'value': option['value']})

    def test_scoped_option_cannot_be_relabelled_as_another_analysis(self):
        original = self.analyse()
        with self.assertRaises(ValueError):
            self.turn(interaction={'slot': flow.CONFIRM_SLOT,
                'value': original['interaction']['options'][0]['value'], 'analysis_id': 'different'})
        for value in ({}, [], True, None):
            with self.assertRaises(ValueError):
                self.confirm(original, value)

    def test_usage_choice_does_not_recall_or_mutate_current_food(self):
        original = self.analyse()
        self.state['slots'].update(food_health_state='none', food_health_stage='none', food_goal='general')
        with patch.object(chat, 'recommend_guardian_foods') as recall:
            result = self.confirm(original)
        recall.assert_not_called()
        self.assertEqual(result['result']['analysis_stage'], 'current')
        self.assertEqual(self.context['current_food']['formula_id'], 1)
        with self.assertRaises(ValueError):
            self.confirm(original)

    def test_cross_pet_user_and_stale_analysis_cannot_confirm(self):
        original = self.analyse()
        saved = deepcopy(self.state)
        for field, value in (('pet_id', 'other-pet'), ('user_id', 'other-user'), ('conversation_id', 'other-c')):
            self.state = deepcopy(saved)
            self.state['slots'][flow.STATE_KEY][field] = value
            with self.assertRaises(ValueError):
                self.confirm(original)
        self.state = saved
        latest = self.turn('帮我分析一下当前粮')
        self.assertNotEqual(latest['result']['analysis_id'], original['result']['analysis_id'])
        with self.assertRaises(ValueError):
            self.confirm(original)

    def test_natural_usage_choice_is_narrow(self):
        self.analyse()
        for text in ('不知道有没有吃', '是否适合吃？', '先分析一下历史粮'):
            self.assertIsNone(flow.confirmation_interaction(text, self.state))
        result = self.turn('准备尝试')
        self.assertEqual(result['result']['analysis_stage'], 'considering')

    def test_followup_after_usage_choice_answers_without_regenerating_or_recalling(self):
        original = self.analyse()
        self.confirm(original, 'current')
        with patch.object(chat, 'recommend_guardian_foods') as recall, patch.object(flow, 'build_analysis_reply') as builder:
            result = self.turn('为什么纤维支持较低需要观察？')
        recall.assert_not_called(); builder.assert_not_called()
        self.assertEqual(result['response_type'], 'text')
        self.assertEqual(result['result']['analysis_id'], original['result']['analysis_id'])
        self.assertEqual(result['result']['food_candidates'], [])

    def test_symptom_analysis_does_not_route_to_food_catalogue(self):
        for message in ('分析一下软便怎么回事', '帮我分析一下掉毛的原因', '比较一下体重变化'):
            self.assertIsNone(flow.named_request(message))

    def test_combined_brand_product_query_falls_back_to_exact_catalogue_identity(self):
        option = {'formula_id': 5, 'catalog_key': 'formula:5', 'brand': '测试牌',
                  'product_name': '粮5', 'label': '测试牌 粮5'}
        with patch.object(flow, 'list_standardized_product_options', side_effect=[{'items': []}, {'items': [option]}]):
            result = self.turn('帮我分析一下测试牌粮5')
        self.assertEqual(result['result']['health_analysis']['subjects'][0]['formula_id'], 5)

    def test_no_current_food_direct_recommendation_retains_existing_questions(self):
        self.context['current_food'] = None
        self.context['pet_profile'].pop('food_formula_id')
        with patch.object(flow, 'build_analysis_reply') as builder, patch.object(chat, 'recommend_guardian_foods') as recall:
            result = self.turn('帮我推荐一款主粮')
        builder.assert_not_called(); recall.assert_not_called()
        self.assertEqual(result['interaction']['slot'], 'food_health_state')
        self.assertNotIn(flow.STATE_KEY, self.state['slots'])

    def test_upload_in_progress_does_not_emit_a_health_report(self):
        self.context['food_submission'] = {'id': 's1', 'recognition_status': 'processing'}
        result = self.turn(attachments=[{'type': 'food_submission', 'submission_id': 's1'}])
        self.assertEqual(result['response_type'], 'processing')
        self.assertNotIn('health_analysis', result['result'])

    def test_upload_does_not_override_existing_urgent_symptom_flow(self):
        self.context['food_submission'] = {'id': 'upload1', 'formula_id': 3, 'recognition_status': 'success',
            'ingredient_composition': '鸭肉，米'}
        result = chat._handle_context_message('u1', self.conversation, '精神明显变差，还便血', self.state,
            attachments=[{'type': 'food_submission', 'submission_id': 'upload1'}],
            intent_result=({'primary_intent': 'symptom_consult', 'slots': {
                'symptom': '便血', 'warning_signs': ['blood_in_stool', 'poor_mental_status']}}, None))
        self.assertEqual(result['response_type'], 'risk_alert')
        self.assertNotIn('health_analysis', result['result'])

    def test_named_food_version_ambiguity_requires_choice(self):
        malformed = chat._default_state('malformed')
        malformed['slots']['food_name'] = ['not-a-product-name']
        with patch.object(flow, 'list_standardized_product_options') as catalog:
            self.assertIsNone(flow.prepare_named_analysis('', malformed, {}, lambda fid: analysis(fid)))
        catalog.assert_not_called()
        options = [{'formula_id': n, 'catalog_key': f'formula:{n}', 'product_name': f'品牌A粮{n}'} for n in (1, 2)]
        with patch.object(flow, 'list_standardized_product_options', return_value={'items': options}):
            result = self.turn('帮我分析一下品牌A')
        self.assertEqual(result['interaction']['slot'], flow.CHOICE_SLOT)
        self.assertNotIn('health_analysis', result['result'])
        result = self.turn(interaction={'slot': flow.CHOICE_SLOT, 'value': 'formula:2'})
        # _build_context is stubbed here; validate identity via the real context path separately.
        self.assertEqual(self.state['slots']['catalog_key'], 'formula:2')
        with self.assertRaises(ValueError):
            self.turn(interaction={'slot': flow.CHOICE_SLOT, 'value': 'formula:999'})


if __name__ == '__main__':
    unittest.main()
