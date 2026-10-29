"""Requirement 39: unified evidence, all analysis entry points and old-client compatibility."""
import json
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from services import guardian_report_service as report
from services import guardian_health_flow_service as flow
from services import miniprogram_chat_service as chat
from services.unified_product_profile_service import build_unified_profile
from test_guardian_health_analysis import portrait
import test_guardian_health_analysis as health_fixtures


def profile(fid=1, **scores):
    return build_unified_profile({'formula_id': fid, 'brand': '测试牌', 'product_name': f'粮{fid}',
                                 'structure_labels': [], 'market_rankings': {}, 'advantage_tags': [], 'weakness_tags': []},
                                 portrait(fid, **scores), {}, 'market-test')


EMPTY_COMMUNITY = {'status': 'no_match', 'items': [], 'source_status': {}, 'note': '社区经验仅供参考'}


class ReportTest(unittest.TestCase):
    def setUp(self):
        for mock in (patch.object(report, 'get_chat_model_config', return_value={'api_key': ''}),
                     patch.object(report, 'community_evidence', return_value=deepcopy(EMPTY_COMMUNITY)),
                     patch.object(report, 'load_profiles', return_value={1: profile(), 2: profile(2, fiber_score=4)})):
            mock.start(); self.addCleanup(mock.stop)

    def build(self, subjects=None, **kwargs):
        subjects = subjects if subjects is not None else [{'formula_id': 1, 'label': '粮1', 'portrait': portrait()}]
        return report.attach_report({'reply': '旧版正文', 'result': {'analysis_id': 'a1'},
                                     'interaction': {'slot': 'food_analysis_usage', 'analysis_id': 'a1',
                                                     'options': [{'value': 'current:a1', 'label': '已经在吃'}]}},
                                    {'pet_profile': {'name': '胖胖', 'breed': '英短', 'age_months': 24}}, subjects,
                                    **kwargs)

    def test_single_summary_and_all_sections_keep_legacy_response_and_scoped_actions(self):
        value = self.build()
        data = value['result']['guardian_report']
        self.assertEqual(value['reply'], '旧版正文')
        self.assertEqual(data['section_order'], ['pet', 'summary', 'feeding', 'community', 'next_steps'])
        self.assertEqual(data['schema'], report.SCHEMA)
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['scene'], 'product_analysis')
        self.assertEqual(data['pet']['life_stage'], '成年期')
        self.assertIn('胖胖', data['summary']['text'])
        self.assertEqual(data['next_steps']['interaction'], value['interaction'])
        self.assertEqual(data['feeding']['products'][0]['source']['table'], report.TABLE)
        self.assertTrue(data['feeding']['products'][0]['strengths'])
        json.dumps(value, allow_nan=False)

    def test_missing_unknown_and_stale_profiles_never_fabricate_advantages(self):
        for subjects in ([], [{'formula_id': 99, 'label': '未关联粮', 'portrait': {}}],
                         [{'formula_id': 1, 'portrait': portrait(fat_score=.9)}]):
            data = self.build(subjects)['result']['guardian_report']
            self.assertEqual(data['status'], 'insufficient_data')
            self.assertTrue(all(not p['strengths'] for p in data['feeding']['products']))
        self.assertIsNone(report.pet_summary({'age_months': None})['life_stage'])
        self.assertIsNone(report.pet_summary({'age_months': float('nan')})['life_stage'])

    def test_differences_are_numeric_same_scale_and_same_version_only(self):
        subjects = [{'formula_id': 1, 'portrait': portrait()}, {'formula_id': 2, 'portrait': portrait(2, fiber_score=4)}]
        comparison = self.build(subjects)['result']['guardian_report']['feeding']['comparisons'][0]
        self.assertEqual(comparison['status'], 'comparable')
        self.assertEqual(next(x['delta'] for x in comparison['dimensions'] if x['key'] == 'fiber'), 3.5)
        subjects[1]['portrait']['calculation_version'] = 'different'
        comparison = self.build(subjects)['result']['guardian_report']['feeding']['comparisons'][0]
        self.assertEqual(comparison['status'], 'not_comparable')
        self.assertTrue(all(x['delta'] is None for x in comparison['dimensions']))

    def test_provisional_upload_cannot_borrow_official_identity_or_market_rank(self):
        data = self.build([{'formula_id': 1, 'label': '上传图片', 'portrait': portrait(),
                            'result_stage': 'provisional', 'result_id': 88, 'quality_flags': ['pending_identity']}],
                          scene='ingredient_analysis')['result']['guardian_report']
        product = data['feeding']['products'][0]
        self.assertEqual(product['status'], 'provisional')
        self.assertEqual(product['result_id'], 88)
        self.assertIsNone(product['brand'])
        self.assertEqual(product['source']['table'], 'calculation_result_snapshot')
        self.assertNotIn('market_version', product['source'])
        self.assertIn('初算', product['note'])

    def test_model_rejects_unknown_evidence_and_times_out_to_fallback(self):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = json.dumps({
            'summary': '整体总结', 'feeding': '喂养建议', 'community': '社区经验', 'source_ids': ['product:999']})
        with patch.object(report, 'get_chat_model_config', return_value={'api_key': 'test', 'base_url': 'http://invalid', 'model': 'test'}), \
             patch.object(report, 'OpenAI', return_value=client):
            self.assertEqual(self.build()['result']['guardian_report']['generation']['mode'], 'evidence_fallback')
            client.chat.completions.create.return_value.choices[0].message.content = json.dumps({
                'summary': '整体总结', 'feeding': '喂养建议', 'community': '社区经验', 'source_ids': ['product:0']})
            self.assertEqual(self.build()['result']['guardian_report']['generation']['mode'], 'model_summary')
            client.chat.completions.create.side_effect = TimeoutError()
            self.assertEqual(self.build()['result']['guardian_report']['generation']['mode'], 'evidence_fallback')


class RepositoryTest(unittest.TestCase):
    def test_profile_read_is_batched_version_pinned_hash_checked_and_read_only(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        good = profile()
        bad = profile(2)
        bad['standalone_assessment']['strengths'] = [{'summary': '被修改的结论'}]
        cur.fetchall.return_value = [{'formula_id': p['formula_id'], 'profile_json': json.dumps(p),
                                     'source_data_hash': p['source_data_hash']} for p in (good, bad)]
        with patch.object(report, '_connect', return_value=conn):
            self.assertEqual(set(report.load_profiles([1, 2, 1])), {1})
        sql, params = cur.execute.call_args.args
        self.assertTrue(sql.strip().startswith('SELECT'))
        self.assertEqual(params, [report.PROFILE_VERSION, 1, 2])
        conn.commit.assert_not_called()

    def test_community_queries_exact_brands_and_labels_brand_scope(self):
        conn = MagicMock()
        cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [{'brand': '品牌A', 'secondary_symptom': '软便', 'sample_count': 9,
                                     'improved': 5, 'worsened': 4}]
        with patch.object(report, '_connect', return_value=conn), \
             patch.object(report.knowledge, 'retrieve', return_value={'status': 'no_match', 'items': []}):
            data = report.community_evidence({'animal_type': 'cat'}, [{'brand': '品牌A', 'cautions': []}])
        self.assertEqual(cur.execute.call_args.args[1], ['品牌A'])
        self.assertEqual(data['items'][0]['source']['scope'], 'brand')
        self.assertIn('不能归因于本配方', data['items'][0]['text'])
        self.assertNotIn('comment_text', cur.execute.call_args.args[0])

    def test_database_outage_is_an_explicit_empty_state(self):
        with patch.object(report, '_connect', side_effect=report.pymysql.OperationalError(2003, 'offline')), \
             patch.object(report.knowledge, 'retrieve', return_value={'status': 'unavailable', 'items': []}):
            self.assertEqual(report.load_profiles([1]), {})
            self.assertEqual(report.community_evidence({}, [{'brand': 'A', 'cautions': []}])['status'], 'unavailable')


class EntryPointTest(health_fixtures.HealthChatTest):
    def setUp(self):
        super().setUp()
        for mock in (patch.object(report, 'get_chat_model_config', return_value={'api_key': ''}),
                     patch.object(report, 'community_evidence', return_value=deepcopy(EMPTY_COMMUNITY)),
                     patch.object(report, 'load_profiles', return_value={i: profile(i) for i in (1, 2, 3)})):
            mock.start(); self.addCleanup(mock.stop)

    def test_saved_diet_history_comparison_and_current_share_protocol(self):
        cases = [('帮我分析一下当前粮', 'product_analysis'), ('分析一下历史吃过的粮', 'diet_history'),
                 ('帮我对比一下当前粮和历史粮', 'product_comparison')]
        for message, scene in cases:
            self.state = chat._default_state('c1')
            response = self.turn(message)
            self.assertEqual(response['result']['guardian_report']['scene'], scene)
            self.assertEqual(response['messages'][0]['result']['guardian_report'], response['result']['guardian_report'])
        response = chat._handle_diet_advice_entry('u1', self.conversation, self.state, 'm1', {'entry_point': 'diet_advice'})
        self.assertEqual(response['result']['guardian_report']['scene'], 'diet_advice')

    def test_upload_with_known_formula_has_ingredient_scene_without_changing_current_food(self):
        self.context['food_submission'] = {'id': 'upload1', 'formula_id': 3, 'recognition_status': 'success',
                                         'ingredient_composition': '鸭肉，米', 'recognized_product_name': '上传粮'}
        response = self.turn(attachments=[{'type': 'food_submission', 'submission_id': 'upload1'}])
        self.assertEqual(response['result']['guardian_report']['scene'], 'ingredient_analysis')
        self.assertEqual(self.context['current_food']['formula_id'], 1)

    def test_versioned_upload_keeps_initial_result_and_current_comparison(self):
        from services import guardian_product_portrait_service as portraits
        from services import formula_calculation_service as calculation
        self.context['food_submission'] = {'id': 'upload1', 'formula_id': 3, 'recognition_status': 'processing',
            'result_available': True, 'claimed_product_name': '上传粮', 'calculation_result': {
                'result_id': 8, 'result_stage': 'provisional', 'portrait': portrait(3), 'quality_flags': ['identity_pending']}}
        with patch.object(portraits, 'load_product_portraits', return_value={1: portrait()}), \
             patch.object(calculation, 'queue_formula_refresh', return_value=None):
            response = self.turn(attachments=[{'type': 'food_submission', 'submission_id': 'upload1'}])
        data = response['result']['guardian_report']
        self.assertEqual(data['scene'], 'ingredient_analysis')
        self.assertEqual(len(data['feeding']['products']), 2)
        self.assertEqual(data['feeding']['products'][0]['roles'], ['current'])
        self.assertEqual(data['feeding']['products'][1]['result_id'], 8)
        self.assertEqual(data['feeding']['products'][1]['status'], 'provisional')
        self.assertIn('初算', data['summary']['text'])
        self.assertEqual(response['result']['result_stage'], 'provisional')

    def test_http_natural_diet_advice_persists_protocol_with_message(self):
        from flask import Flask
        from importlib import import_module
        api = import_module('api.miniprogram_api')
        app = Flask(__name__)
        app.register_blueprint(api.miniprogram_api)
        saved = []
        def save(**values):
            saved.append(deepcopy(values))
            return {'id': str(len(saved)), **values}
        with patch.object(api, '_user_id_from_request', return_value='u1'), \
             patch.object(chat, '_get_conversation', return_value=self.conversation), \
             patch.object(chat, '_load_state', return_value=self.state), \
             patch.object(chat, '_save_message', side_effect=save), patch.object(chat, '_save_state'), \
             patch.object(chat, '_connect', return_value=MagicMock()), \
             patch.object(chat, '_recognize_guardian_turn') as recognize:
            response = app.test_client().post('/api/miniprogram/chat/conversations/c1/messages', json={'message': '给我日常饮食建议'})
        self.assertEqual(response.status_code, 200)
        recognize.assert_not_called()
        data = response.get_json()['message']['result']['guardian_report']
        self.assertEqual(data['scene'], 'diet_advice')
        self.assertEqual(data, saved[-1]['result']['guardian_report'])


if __name__ == '__main__':
    unittest.main()
