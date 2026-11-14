import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from services import multi_model_prompt_service as prompts
from api.enterprise_api import enterprise_api


class PromptTemplatesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.seed = root / 'knowledge_bases.json'
        (root / 'one.txt').write_text('问题：{{question}}')
        (root / 'two.txt').write_text('分析：{{question}}')
        self.seed.write_text(json.dumps({'default_base': 'selection', 'bases': [{'code': 'selection', 'label': '选粮知识库', 'templates': [
            {'code': 'one', 'title': '默认模板', 'description': '', 'file': 'one.txt'},
            {'code': 'two', 'title': '比较模板', 'description': '', 'file': 'two.txt'}]}]}))
        self.paths = patch.multiple(prompts, BASE_CONFIG_PATH=self.seed, SETTINGS_PATH=root / 'var' / 'templates.json')
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.addCleanup(self.directory.cleanup)
        app = Flask(__name__)
        app.secret_key = 'local-test-only'
        app.register_blueprint(enterprise_api)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['workbench_user'] = {'role': 'data_admin'}

    def payload(self, **extra):
        return {'knowledge_base': 'selection', 'revision': prompts.list_templates()['revision'],
                'title': '新模板', 'description': '用于测试', 'template': '测试 {{question}}',
                'active': True, 'is_default': False, **extra}

    def test_create_persists_and_can_be_default(self):
        result = prompts.save_template(self.payload(is_default=True))
        self.assertEqual(prompts.list_templates()['revision'], result['revision'])
        self.assertEqual(prompts.collection_bases()['bases'][0]['templates'][0]['code'], result['saved_code'])
        self.assertEqual((self.seed.parent / 'one.txt').read_text(), '问题：{{question}}')

    def test_edit_does_not_change_previous_snapshot(self):
        old = prompts.collection_bases()['bases'][0]['templates'][0]['template']
        prompts.save_template(self.payload(template='新版 {{question}}'), base_code='selection', template_code='one')
        self.assertEqual(old, '问题：{{question}}')
        self.assertEqual(prompts.collection_bases()['bases'][0]['templates'][0]['template'], '新版 {{question}}')

    def test_disable_default_selects_another_active_template(self):
        prompts.save_template(self.payload(active=False), base_code='selection', template_code='one')
        self.assertEqual(prompts.collection_bases()['bases'][0]['default_template'], 'two')
        self.assertEqual(len(prompts.collection_bases()['bases'][0]['templates']), 1)
        self.assertEqual(len(prompts.list_templates()['bases'][0]['templates']), 2)

    def test_last_active_template_cannot_be_disabled(self):
        prompts.save_template(self.payload(active=False), base_code='selection', template_code='one')
        with self.assertRaisesRegex(ValueError, '至少保留'):
            prompts.save_template(self.payload(active=False), base_code='selection', template_code='two')
        self.assertEqual(len(prompts.collection_bases()['bases'][0]['templates']), 1)

    def test_revision_rejects_stale_writes(self):
        stale = self.payload()
        prompts.save_template(stale)
        with self.assertRaises(prompts.TemplateConflict):
            prompts.save_template(stale)

    def test_missing_placeholder_rejected(self):
        with self.assertRaisesRegex(ValueError, 'question'):
            prompts.save_template(self.payload(template='没有问题占位符'))
        self.assertFalse(prompts.SETTINGS_PATH.exists())

    def test_unknown_base_and_template_rejected(self):
        with self.assertRaises(LookupError):
            prompts.save_template(self.payload(knowledge_base='unknown'))
        with self.assertRaises(LookupError):
            prompts.save_template(self.payload(), base_code='selection', template_code='unknown')

    def test_api_create_update_and_conflict(self):
        data = self.client.get('/api/admin/multi-model/templates').get_json()
        self.assertTrue(data['ok'])
        payload = self.payload()
        response = self.client.post('/api/admin/multi-model/templates', json=payload)
        self.assertEqual(response.status_code, 200)
        code = response.get_json()['saved_code']
        self.assertEqual(self.client.put(f'/api/admin/multi-model/templates/selection/{code}', json=payload).status_code, 409)
        payload = self.payload(title='修改名称')
        self.assertEqual(self.client.put(f'/api/admin/multi-model/templates/selection/{code}', json=payload).status_code, 200)

    def test_collection_resolves_updated_template_and_rejects_disabled(self):
        from services import multi_model_knowledge_service as multi
        from vendor.chongxi_multi_model_mysql.run import Provider
        prompts.save_template(self.payload(template='采集快照 {{question}}'), base_code='selection', template_code='one')
        payload = {'knowledge_base': 'selection', 'question': '测试问题', 'template_code': 'one', 'providers': ['qwen']}
        with patch.object(multi.collector, 'provider_configuration', return_value=[Provider('qwen', 'test', '', 'test-model')]):
            self.assertEqual(multi.validate(payload)[1], '采集快照 {{question}}')
            prompts.save_template(self.payload(active=False), base_code='selection', template_code='one')
            with self.assertRaisesRegex(ValueError, '模版'):
                multi.validate(payload)

    def test_concurrent_writers_cannot_overwrite_each_other(self):
        from concurrent.futures import ThreadPoolExecutor
        payload = self.payload()
        def save():
            try:
                prompts.save_template(payload)
                return 'saved'
            except prompts.TemplateConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: save(), range(2)))
        self.assertCountEqual(results, ['saved', 'conflict'])
        self.assertEqual(len(prompts.list_templates()['bases'][0]['templates']), 3)

    def test_api_requires_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
        with patch.dict('os.environ', {'MINIPROGRAM_ADMIN_TOKEN': 'test-only'}):
            for method in ['get', 'post']:
                response = getattr(self.client, method)('/api/admin/multi-model/templates', json=self.payload())
                self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
