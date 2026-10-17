import unittest
import json
import os
import time
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from services.guardian_presentation import display_blocks, present_message
from services import submission_notifications as notices
from services import orchestrator_worker as worker
from services import orchestrator_service as jobs
from services import miniprogram_food_submission_service as submissions


class SubmissionAutomationTests(unittest.TestCase):
    def test_portrait_node_is_required_between_scores_and_risks(self):
        definitions = {node.node_code: node for node in jobs.get_pipeline_definition('catfood_image_analysis')}
        portrait = definitions['product_portrait_summary']
        self.assertEqual(portrait.depends_on, ('ingredient_standardize',))
        self.assertEqual(definitions['formula_profile'].depends_on, ('product_portrait_summary',))

    def test_display_preserves_uncertainty_and_explicit_semantics(self):
        blocks = display_blocks('✅ **结构接近**：有比较依据。\n❗ **资料不足**：不能确认效果。')
        self.assertEqual([b['kind'] for b in blocks], ['positive', 'caution'])
        self.assertTrue(blocks[0]['segments'][0]['strong'])
        self.assertEqual(blocks[1]['segments'][-1]['text'], '：不能确认效果。')

    def test_plain_unknown_is_not_classified_as_negative(self):
        self.assertEqual(display_blocks('尚无资料，不能判断。')[0]['kind'], 'body')

    def test_numbered_model_points_keep_explicit_semantic_markers(self):
        blocks = display_blocks('1. ✅ **抓板位置**：放在经常活动的地方。\n2. ❗ **先观察**：留意猫咪是否愿意靠近。')
        self.assertEqual([b['marker'] for b in blocks], ['✅', '❗'])
        self.assertEqual(blocks[0]['segments'][0], {'text': '抓板位置', 'strong': True})

    def test_incomplete_assistant_emphasis_does_not_leak_markdown_delimiters(self):
        blocks = display_blocks('❗ **需要观察，但这段标记没有闭合。')
        self.assertNotIn('**', ''.join(s['text'] for b in blocks for s in b['segments']))

    def test_long_reply_splits_without_losing_content(self):
        text = ('这是需要保留的观察信息。' * 40)
        blocks = display_blocks(text)
        self.assertGreater(len(blocks), 1)
        self.assertEqual(''.join(s['text'] for b in blocks for s in b['segments']), text)

    def test_rule_reply_is_split_into_two_sentence_paragraphs(self):
        text = '需要优先评估。尽快联系宠物医院。记录变化。带上已有资料。'
        blocks = display_blocks(text, 'risk_alert')
        self.assertEqual(len(blocks), 2)
        self.assertTrue(all(b['kind'] == 'caution' for b in blocks))
        self.assertTrue(any(s['strong'] for s in blocks[0]['segments']))
        self.assertEqual(''.join(s['text'] for b in blocks for s in b['segments']), text)

    def test_user_message_is_not_formatted_and_assistant_interaction_is_preserved(self):
        user = {'role': 'user', 'content': '**原文**'}
        self.assertEqual(present_message(user), user)
        msg = {'role': 'assistant', 'content': '❗ **观察**：继续记录。',
               'interaction': {'slot': 'x'}, 'result': {'turn_no': 2}}
        shown = present_message(msg)
        self.assertEqual(shown['interaction'], msg['interaction'])
        self.assertEqual(shown['result']['turn_no'], 2)

    def test_template_configuration_is_not_guessed(self):
        with patch.dict('os.environ', {'WECHAT_FOOD_RESULT_TEMPLATE_ID': 'id',
                                      'WECHAT_FOOD_RESULT_TEMPLATE_FIELDS': '{}'}):
            self.assertFalse(notices.config()['enabled'])
        with patch.dict('os.environ', {'WECHAT_FOOD_RESULT_TEMPLATE_ID': 'id',
                                      'WECHAT_FOOD_RESULT_TEMPLATE_FIELDS': '{"thing1":"product","phrase2":"status"}'}):
            self.assertTrue(notices.config()['enabled'])

    def test_template_text_limits_and_beijing_time(self):
        data = notices.template_data({'claimed_product_name': '粮' * 60,
                                      'finished_at': datetime(2026, 10, 16, 0, 30, tzinfo=timezone.utc)},
                                     {'thing1': 'product', 'phrase2': 'status', 'time3': 'completed_at'})
        self.assertEqual(len(data['thing1']['value']), 20)
        self.assertEqual(data['time3']['value'], '2026-10-16 08:30')

    def test_real_analysis_template_fields_and_product_summary(self):
        fields = {'thing1': 'analysis_type', 'thing3': 'result_summary', 'time4': 'completed_at'}
        with patch.dict('os.environ', {'WECHAT_FOOD_RESULT_TEMPLATE_ID': 'configured-id',
                                      'WECHAT_FOOD_RESULT_TEMPLATE_FIELDS': json.dumps(fields)}):
            self.assertTrue(notices.config()['enabled'])
        data = notices.template_data({'claimed_product_name': '粮' * 30,
                                     'finished_at': datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc)}, fields)
        self.assertEqual(data['thing1']['value'], '食品配料解析')
        self.assertEqual(data['thing3']['value'], '粮' * 16 + '解析完成')
        self.assertEqual(len(data['thing3']['value']), 20)
        self.assertEqual(data['time4']['value'], '2026-10-17 08:30')
        with patch.dict('os.environ', {'WECHAT_FOOD_RESULT_TEMPLATE_ID': 'configured-id',
                                      'WECHAT_FOOD_RESULT_TEMPLATE_FIELDS': '{"phrase1":"analysis_type"}'}):
            self.assertFalse(notices.config()['enabled'])

    def test_no_notification_network_when_template_is_unconfigured(self):
        with patch.object(notices, 'config', return_value={'enabled': False}), patch.object(notices, '_post') as post:
            notices.reconcile()
            post.assert_not_called()

    def test_only_known_pre_execution_rejections_are_retried(self):
        self.assertTrue(worker.retryable({'status_code': 429}))
        self.assertTrue(worker.retryable({'status_code': 409, 'data': {'code': 'market_profile_busy'}}))
        self.assertFalse(worker.retryable({'status_code': 500, 'data': {'error': 'timeout'}}))

    def test_old_worker_callback_cannot_overwrite_restarted_node(self):
        task = {'id': 't', 'task_status': 'processing',
                'nodes': [{'node_code': 'ocr_formula', 'claim_token': 'worker:new'}]}
        with patch.object(jobs, 'get_task', return_value=task), patch.object(jobs, '_connect') as connect:
            with self.assertRaisesRegex(ValueError, '其他执行轮次'):
                jobs.apply_node_result('t', 'ocr_formula', call_status='success', output={'x': 1}, claim_token='worker:old')
            connect.assert_not_called()

    def test_cancelled_task_ignores_late_result(self):
        task = {'id': 't', 'task_status': 'cancelled', 'nodes': [{'node_code': 'ocr_formula'}]}
        with patch.object(jobs, 'get_task', return_value=task), patch.object(jobs, '_connect') as connect:
            self.assertEqual(jobs.apply_node_result('t', 'ocr_formula', call_status='success'), task)
            connect.assert_not_called()

    def test_incomplete_science_stays_waiting_for_review(self):
        checked = jobs.output_check('science_feature_materialize', {'ok': False, 'status': 'need_review'})
        self.assertEqual(checked.status, jobs.NODE_NEED_REVIEW)
        self.assertFalse(checked.passed)

    def test_science_partial_or_unrecognized_status_does_not_complete(self):
        for status in ['partial', 'partially_materialized']:
            with self.subTest(status=status):
                result = jobs.output_check('science_feature_materialize', {'status': status, 'formula_id': 1})
                self.assertFalse(result.passed)
                self.assertEqual(result.status, jobs.NODE_NEED_REVIEW)
        self.assertFalse(jobs.output_check('science_feature_materialize', {'status': 'unexpected'}).passed)
        self.assertTrue(jobs.output_check('science_feature_materialize', {'status': 'materialized'}).passed)

    def test_local_upload_check_returns_its_execution_token(self):
        app, conn = MagicMock(), MagicMock()
        conn.__enter__.return_value = conn
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {'acquired': 1}
        cur.fetchall.return_value = []
        task = {'id': 't', 'task_status': 'created', 'payload': {'image_path': '/test/package.jpg'}}
        job = {'node_code': 'upload_check', 'api': None, 'claim_token': 'worker:check'}
        with patch.object(jobs, '_connect', return_value=conn), \
                patch.object(jobs, 'get_task', side_effect=[task, {'task_status': 'done'}]), \
                patch.object(jobs, 'claim_ready_dispatch_jobs', return_value={'jobs': [job]}), \
                patch.object(jobs, 'apply_node_result') as report, \
                patch.object(worker._stop, 'is_set', return_value=False):
            worker._execute_task(app, 't')
        report.assert_called_once_with('t', 'upload_check', call_status='success',
                                       output=task['payload'], claim_token='worker:check')

    def test_duplicate_approval_resumes_without_claiming_calculation_is_complete(self):
        from flask import Flask
        from api import product_identity_api as identity_api
        app = Flask(__name__)
        app.register_blueprint(identity_api.product_identity_api)
        approved = {'ok': True, 'identity_mode': 'duplicate', 'source_ids': [7], 'pipeline_complete': True}
        with patch.object(identity_api, 'review_product_candidate', return_value=approved), \
                patch.object(identity_api, 'complete_duplicate_formula_tasks', return_value=['task-7']):
            response = app.test_client().post('/api/product-identity/product-reviews/3/approve', json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['resumed_task_ids'], ['task-7'])
        self.assertEqual(response.json['completed_task_ids'], [])
        self.assertFalse(response.json['pipeline_complete'])

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires process timezone support')
    def test_task_completion_clock_is_utc_in_a_beijing_process(self):
        try:
            with patch.dict(os.environ, {'TZ': 'Asia/Shanghai'}):
                time.tzset()
                recorded = datetime.strptime(jobs.utc_now(), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                self.assertLess(abs((recorded - datetime.now(timezone.utc)).total_seconds()), 2)
        finally:
            time.tzset()

    def test_subscription_checks_owner_before_writing(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = None
        with patch.object(notices, 'config', return_value={'enabled': True}), \
                patch.object(notices, 'init_db'), patch.object(jobs, '_connect', return_value=conn):
            with self.assertRaises(LookupError):
                notices.subscribe('other-user', 'private-id', 'accept')
        self.assertEqual(cur.execute.call_count, 1)

    def test_customer_progress_does_not_expose_internal_errors(self):
        task = {'task_status': 'failed', 'error_message': 'DASHSCOPE_API_KEY missing',
                'nodes': [{'node_code': 'ocr_formula', 'node_name': '配料表识别',
                           'node_status': 'failed', 'error_message': 'private database host'}]}
        with patch.object(submissions, 'get_orchestrator_task', return_value=task), \
                patch.object(submissions, '_connect') as connect:
            connect.return_value.__enter__.return_value = MagicMock()
            result = submissions._sync_recognition({'id': 's', 'status': 'active', 'orchestrator_task_id': 't'})
        self.assertEqual(result['processing_status'], 'failed')
        self.assertIn('请稍后重试', result['processing_message'])
        self.assertNotIn('DASHSCOPE', result['processing_message'])
        self.assertNotIn('private', result['processing_message'])

    def test_submission_uses_confirmed_identity_over_ocr_filename_fallback(self):
        task = {'task_status': 'done', 'outputs': {
            'ocr_formula': {'brand': 'Product OCR', 'product_name': 'OCR abcd1234',
                            'ingredient_composition': '鸡肉、鱼油'},
            'brand_standardize': {'standard_brand_name': '标准品牌'},
            'product_standardize': {'product_id': 7},
        }, 'nodes': [{'node_code': code, 'node_status': 'success'}
                     for code in ['ocr_formula', 'brand_standardize', 'product_standardize']]}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {'standard_product_name': '标准产品'}
        with patch.object(submissions, 'get_orchestrator_task', return_value=task), \
                patch.object(submissions, '_connect', return_value=conn):
            result = submissions._sync_recognition({'id': 's', 'status': 'active', 'orchestrator_task_id': 't'})
        self.assertEqual(result['recognized_brand'], '标准品牌')
        self.assertEqual(result['recognized_product_name'], '标准产品')
        self.assertTrue(result['result_available'])

    def test_submission_sync_persists_formula_catalog_identity(self):
        task = {'task_status': 'done', 'outputs': {
            'ocr_formula': {'source_id': 514, 'ingredient_composition': '鸡肉、鱼油'},
            'brand_standardize': {'standard_brand_name': '渴望'},
            'product_standardize': {'product_id': 174},
            'formula_standardize': {'product_id': 174, 'formula_id': 327},
        }, 'nodes': [{'node_code': code, 'node_status': 'success'} for code in (
            'ocr_formula', 'brand_standardize', 'product_standardize', 'formula_standardize',
            'product_portrait_summary',
        )]}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
            'standard_product_name': '八重守护',
        }
        with patch.object(submissions, 'get_orchestrator_task', return_value=task), \
                patch.object(submissions, '_connect', return_value=conn):
            result = submissions._sync_recognition(
                {'id': 'submission', 'status': 'active', 'orchestrator_task_id': 'task'}
            )
        self.assertEqual(result['standard_product_id'], 174)
        self.assertEqual(result['formula_id'], 327)
        self.assertEqual(result['catalog_key'], 'formula:327')
        self.assertEqual(result['review_status'], 'approved')
        self.assertEqual(result['publish_status'], 'published')

    def test_submission_stays_pending_until_product_portrait_succeeds(self):
        task = {'task_status': 'need_review', 'outputs': {
            'ocr_formula': {'ingredient_composition': '鸡肉、鱼油'},
            'formula_standardize': {'product_id': 174, 'formula_id': 327},
        }, 'nodes': [
            {'node_code': 'ocr_formula', 'node_status': 'success'},
            {'node_code': 'formula_standardize', 'node_status': 'success'},
            {'node_code': 'product_portrait_summary', 'node_status': 'need_review'},
        ]}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        with patch.object(submissions, 'get_orchestrator_task', return_value=task), \
                patch.object(submissions, '_formula_portrait_exists', return_value=False), \
                patch.object(submissions, '_connect', return_value=conn):
            result = submissions._sync_recognition({
                'id': 'submission', 'status': 'active', 'orchestrator_task_id': 'task',
                'review_status': 'pending', 'publish_status': 'unpublished',
            })
        self.assertEqual(result['catalog_key'], 'formula:327')
        self.assertEqual(result['review_status'], 'pending')
        self.assertEqual(result['publish_status'], 'unpublished')

    def test_pipeline_does_not_reverse_an_explicit_rejection(self):
        task = {'task_status': 'done', 'outputs': {
            'ocr_formula': {'ingredient_composition': '鸡肉、鱼油'},
            'formula_standardize': {'product_id': 174, 'formula_id': 327},
        }, 'nodes': [
            {'node_code': 'ocr_formula', 'node_status': 'success'},
            {'node_code': 'formula_standardize', 'node_status': 'success'},
            {'node_code': 'product_portrait_summary', 'node_status': 'success'},
        ]}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        with patch.object(submissions, 'get_orchestrator_task', return_value=task), \
                patch.object(submissions, '_connect', return_value=conn):
            result = submissions._sync_recognition({
                'id': 'submission', 'status': 'active', 'orchestrator_task_id': 'task',
                'review_status': 'rejected', 'publish_status': 'unpublished',
            })
        self.assertEqual(result['review_status'], 'rejected')
        self.assertEqual(result['publish_status'], 'unpublished')

    def test_legacy_submission_completes_when_formula_portrait_already_exists(self):
        task = {'task_status': 'done', 'outputs': {
            'ocr_formula': {'ingredient_composition': '鸡肉、鱼油'},
            'formula_standardize': {'product_id': 174, 'formula_id': 327},
        }, 'nodes': [
            {'node_code': 'ocr_formula', 'node_status': 'success'},
            {'node_code': 'formula_standardize', 'node_status': 'success'},
        ]}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        with patch.object(submissions, 'get_orchestrator_task', return_value=task), \
                patch.object(submissions, '_formula_portrait_exists', return_value=True), \
                patch.object(submissions, '_connect', return_value=conn):
            result = submissions._sync_recognition({
                'id': 'legacy', 'status': 'active', 'orchestrator_task_id': 'task',
                'review_status': 'pending', 'publish_status': 'unpublished',
            })
        self.assertEqual(result['catalog_key'], 'formula:327')
        self.assertEqual(result['review_status'], 'approved')
        self.assertEqual(result['publish_status'], 'published')

    def test_cancelled_submission_does_not_show_queued_progress(self):
        with patch.object(submissions, 'get_orchestrator_task') as get_task:
            result = submissions._sync_recognition({'id': 's', 'status': 'cancelled', 'orchestrator_task_id': 't'})
        get_task.assert_not_called()
        self.assertEqual(result['processing_status'], 'cancelled')
        self.assertEqual(result['processing_stage'], '已撤销')
        self.assertFalse(result['result_available'])


if __name__ == '__main__':
    unittest.main()
