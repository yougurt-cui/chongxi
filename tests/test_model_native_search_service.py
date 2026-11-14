import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

from services import model_native_search_service as search


class NativeSearchTests(unittest.TestCase):
    def provider(self, name='doubao', host='ark.cn-beijing.volces.com'):
        return SimpleNamespace(name=name, base_url='https://'+host+'/api/v3',
                               model='doubao-seed-2-1-turbo-260628', key='test-key')

    def call_with(self, body, status=200):
        response = Mock(status_code=status)
        response.json.return_value = body
        with patch.object(search.requests, 'post', return_value=response) as post:
            result = search.call(self.provider(), 'question')
        return result, post.call_args

    def test_ark_payload_and_citations(self):
        body = {'id':'response-test','status':'completed','output':[
            {'type':'web_search_call','status':'completed','action':{'query':'cats'}},
            {'type':'message','content':[{'type':'output_text','text':'{}','annotations':[
                {'type':'url_citation','url':'https://example.org/cats','title':'Cats'}]}]}],
            'usage':{'input_tokens':12,'output_tokens':5,'tool_usage':{'web_search':1}}}
        (text, usage, meta), args = self.call_with(body)
        payload = args.kwargs['json']
        self.assertEqual(payload['tools'], [{'type':'web_search'}])
        self.assertEqual(payload['thinking'], {'type':'disabled'})
        self.assertNotIn('reasoning', payload)
        self.assertTrue(args.args[0].endswith('/responses'))
        self.assertEqual(meta['status'], 'searched')
        self.assertEqual(meta['sources'], [{'url':'https://example.org/cats','title':'Cats'}])
        self.assertEqual(usage['prompt_tokens'],12)
        self.assertEqual(text,'{}')

    def test_usage_confirms_search_without_calls(self):
        (_, _, meta), _ = self.call_with({'output':[], 'usage':{'tool_usage':{'web_search':1}}})
        self.assertEqual(meta['status'], 'no_sources')

    def test_links_alone_do_not_confirm_search(self):
        (_, _, meta), _ = self.call_with({'output':[{'type':'message','content':[
            {'type':'output_text','text':'https://example.org','annotations':[]}]}]})
        self.assertFalse(meta['performed'])
        self.assertEqual(meta['status'], 'not_confirmed')

    def test_activation_error(self):
        for status, code in [(404,'ToolNotOpen'),(403,'AccessDenied')]:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError,'联网内容插件未开通'):
                self.call_with({'error':{'code':code}}, status)

    def test_supported_hosts(self):
        self.assertTrue(search.supports_search(self.provider()))
        self.assertFalse(search.supports_search(self.provider(host='example.org')))
        self.assertTrue(search.supports_search(self.provider('deepseek','api.deepseek.com')))
        self.assertTrue(search.supports_search(self.provider('qwen','dashscope.aliyuncs.com')))

    def test_search_model_snapshot(self):
        with patch.dict('os.environ',{'DOUBAO_SEARCH_MODEL':'different-model'}):
            self.assertEqual(search.search_model(self.provider()), 'different-model')
            with self.assertRaisesRegex(ValueError,'模型快照'):
                search.call(self.provider(),'question')


if __name__ == '__main__':
    unittest.main()
