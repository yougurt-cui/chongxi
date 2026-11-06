import json
from unittest.mock import MagicMock,patch
from services import miniprogram_chat_service as chat
from services import guardian_health_flow_service as flow


def state():
    return {'primary_intent': 'food_analysis', 'slots': {flow.STATE_KEY: {
        'analysis_id': 'a1', 'subjects': [{'label': '蓝氏 猎鸟乳鸽2.0', 'formula_id': 290}]}}}


def test_natural_question_calls_chat_model_with_previous_product_context():
    client = MagicMock()
    client.chat.completions.create.return_value.choices[0].message.content = json.dumps({'reply': '模型解释身体影响，不是固定指标文案。'})
    cfg={'api_key': 'test', 'model': 'chat-test', 'base_url': 'https://example.com'}
    with patch.object(chat,'get_chat_model_config',return_value=cfg),patch.object(chat,'OpenAI',return_value=client):
        text,model=chat._generate_answer('益生元偏低会对猫咪身体产生哪些影响',state(),{},False)
    assert text=='模型解释身体影响，不是固定指标文案。'
    assert model=='chat-test'
    payload=json.loads(client.chat.completions.create.call_args.kwargs['messages'][1]['content'])
    assert payload['previous_product_analysis']['subjects'][0]['formula_id']==290
    assert payload['question']=='益生元偏低会对猫咪身体产生哪些影响'


def test_model_failure_is_explicit_not_generic_symptom_care():
    cfg={'api_key': 'test', 'model': 'chat-test', 'base_url': 'https://example.com'}
    with patch.object(chat,'get_chat_model_config',return_value=cfg),patch.object(chat,'OpenAI',side_effect=RuntimeError):
        text,model=chat._generate_answer('纤维偏低有什么影响',state(),{},False)
    assert model is None
    assert '未能生成回答' in text
    assert '环境变化' not in text and '宠物医院' not in text


def test_chat_followup_uses_general_model_path_instead_of_factor_template():
    import test_guardian_health_analysis as fixtures
    fixture=fixtures.HealthChatTest(methodName='runTest');fixture.setUp()
    try:
        fixture.analyse()
        with patch.object(chat,'_generate_answer',return_value=('模型回答', 'chat-test')) as generate:
            result=fixture.turn('益生元支持偏低有什么问题')
        generate.assert_called_once()
        assert result['reply']=='模型回答'
        assert result['result']['model_name']=='chat-test'
        assert 'factor_explanation' not in result['result']
    finally:fixture.doCleanups()


def test_busy_primary_uses_configured_backup_model():
    primary={'provider':'deepseek','api_key':'test','model':'primary','base_url':'https://example.com'}
    backup={'provider':'qwen','api_key':'test','model':'qwen-plus','base_url':'https://example.com'}
    error=RuntimeError('busy');error.status_code=503
    first,second=MagicMock(),MagicMock()
    first.chat.completions.create.side_effect=error
    second.chat.completions.create.return_value.choices[0].message.content=json.dumps({'reply':'备用大模型的实际回答'})
    with patch.object(chat,'get_chat_model_config',side_effect=[primary,backup]), \
         patch.object(chat,'OpenAI',side_effect=[first,second]):
        text,model=chat._generate_answer('益生元偏低有什么影响',state(),{},False)
    assert text=='备用大模型的实际回答'
    assert model=='qwen-plus'
