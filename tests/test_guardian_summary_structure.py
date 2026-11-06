import json
from unittest.mock import MagicMock, patch
from services import guardian_report_service as report


def products():
    return [{'id': 'product:0', 'source': {'table': 'test'}, 'label': '皇家 BK34',
             'structure': {'primary_type': '多肉源+谷物淀粉型'},
             'strengths': [{'summary': '蛋白质量较高'}], 'cautions': [{'summary': '益生元支持偏低'}]}]


def test_fallback_retains_structure_strength_and_caution():
    with patch.object(report, 'get_chat_model_config', return_value={'api_key': ''}):
        result = report.summarize({'name': '小猫'}, products(), [], {'items': [], 'status': 'no_match'}, 'product_analysis')
    assert '多肉源+谷物淀粉型' in result['summary']
    assert '蛋白质量较高' in result['summary']
    assert '益生元支持偏低' in result['summary']
    assert len(result['summary']) <= 80


def test_model_omitting_existing_structure_uses_grounded_summary():
    client = MagicMock()
    client.chat.completions.create.return_value.choices[0].message.content = json.dumps({
        'summary': '蛋白质量较高，益生元支持偏低。', 'feeding': '观察便便和食欲。',
        'community': '暂无相关社区经验。', 'source_ids': ['product:0']})
    with patch.object(report, 'get_chat_model_config', return_value={'api_key': 'test', 'model': 'test', 'base_url': 'https://example.com'}), \
         patch.object(report, 'OpenAI', return_value=client):
        result = report.summarize({}, products(), [], {'items': [], 'status': 'no_match'}, 'product_analysis')
    assert '多肉源+谷物淀粉型' in result['summary']
    assert result['feeding'] == '观察便便和食欲。'


def test_missing_or_placeholder_structure_is_silently_omitted():
    for structure in ({}, {'headline': '结构资料不足'}):
        items = products()
        items[0]['structure'] = structure
        with patch.object(report, 'get_chat_model_config', return_value={'api_key': ''}):
            result = report.summarize({}, items, [], {'items': [], 'status': 'no_match'}, 'product_analysis')
        assert '结构' not in result['summary']
        assert '蛋白质量较高' in result['summary']
        assert '益生元支持偏低' in result['summary']
        assert len(result['summary']) < 50
