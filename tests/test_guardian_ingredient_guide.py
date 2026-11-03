import pytest

from services import guardian_ingredient_guide as guide
from services import miniprogram_food_submission_service as submissions


def entry(node='ocr_formula'):
    state = {'slots': {}}
    reply = guide.start({'id': 'upload', 'failure_node': node}, state, 'cat', 'entry')
    return state['slots'][guide.SLOT], reply


@pytest.mark.parametrize('task_status, nodes, recognition, expected', [
    ('processing', [('upload_check', 'success'), ('ocr_formula', 'running')], 'processing', None),
    ('need_review', [('ocr_formula', 'need_review'), ('ingredient_standardize', 'blocked')], 'unrecognized', 'ocr_formula'),
    ('failed', [('ocr_formula', 'success'), ('ingredient_standardize', 'failed')], 'success', 'ingredient_standardize'),
    ('waiting_input', [('upload_check', 'blocked'), ('ocr_formula', 'pending')], 'processing', 'upload_check'),
    ('cancelled', [('ocr_formula', 'failed')], 'failed', None),
])
def test_failure_node(task_status, nodes, recognition, expected):
    task = {'task_status': task_status, 'nodes': [dict(node_code=k, node_status=v) for k, v in nodes]}
    assert submissions._failure_node(task, recognition) == expected


@pytest.mark.parametrize('topic', guide.TEMPLATES)
def test_each_template_survives_model_failure(monkeypatch, topic):
    state, first = entry()
    monkeypatch.setattr(guide, 'generate', lambda *args: {})
    question = guide.TEMPLATES[topic]['question']
    option = next(o for o in first['interaction']['options'] if o['label'] == question)
    with pytest.raises(ValueError):
        guide.selected_question(state, 'other-pet', option['value'])
    assert guide.selected_question(state, 'cat', option['value']) == question
    card = guide.advance(state, question, {}, 'turn2')
    assert len(card['result']['ingredient_guide']['sections']) == 3
    assert len(set(state['questions'])) == 3
    with pytest.raises(ValueError):
        guide.selected_question(state, 'cat', option['value'])


def test_generated_questions_use_previous_answer_and_filter_repeats(monkeypatch):
    state, _ = entry()
    seen = []
    def model(g, q, text, context):
        seen.append(list(g['history']))
        return {'answer': '参照包装上的保证分析值。', 'questions': [q, '看哪一项？', '看哪一项?', 12]}
    monkeypatch.setattr(guide, 'generate', model)
    template = guide.advance(state, guide.TEMPLATES['method']['question'], {}, 'second')
    assert state['questions'][0] == '看哪一项？' and len(set(state['questions'])) == 3
    answer = guide.advance(state, state['questions'][0], {}, 'third')
    assert seen[1][0]['answer'] == template['reply']
    assert answer['reply'] == '参照包装上的保证分析值。'
    assert '看哪一项？' not in state['questions']


def test_review_queue_failure_preserves_entry_without_claiming_delivery(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError('queue offline')
    monkeypatch.setattr(guide, 'record_exception', fail)
    _, result = entry('ingredient_standardize')
    assert not result['result']['ingredient_guide']['reupload']
    assert '已提交' not in result['reply'] and len(result['interaction']['options']) == 3


@pytest.mark.parametrize('node,action', [('ocr_formula', '补拍'), ('ingredient_standardize', '后台核对')])
def test_unverified_upload_value_preserves_node_action(monkeypatch, node, action):
    monkeypatch.setattr(guide, 'record_exception', lambda **kwargs: {})
    state, _ = entry(node)
    monkeypatch.setattr(guide, 'generate', lambda *args: {'answer': '脂肪是99%，请等待。'})
    message = guide.advance(state, '刚上传的新粮脂肪是多少？', {}, 'next')
    assert action in message['reply'] and '99%' not in message['reply']
    assert state['history'][-1]['answer'] == message['reply']


def test_other_topic_handoff_keeps_guide_unmodified(monkeypatch):
    from copy import deepcopy
    state, _ = entry()
    before = deepcopy(state)
    monkeypatch.setattr(guide, 'generate', lambda *args: {'other_topic': True})
    assert guide.advance(state, '猫抓板放在哪里好？', {}, 'next', allow_handoff=True) is None
    assert state == before


def test_entry_and_method_match_reference_card(monkeypatch):
    state, card = entry()
    assert all(o.get('subtitle') and o.get('icon') for o in card['interaction']['options'])
    monkeypatch.setattr(guide, 'generate', lambda *args: {'questions': ['建议你先重拍', 'abc?', '只有这个成分']})
    reply = guide.advance(state, card['interaction']['options'][0]['label'], {}, 'next')
    design = reply['result']['ingredient_guide']
    assert design['summary'] in reply['reply']
    assert all(section['example'] in reply['reply'] for section in design['sections'])
    assert state['questions'] == guide.TEMPLATES['method']['fallback']



@pytest.mark.parametrize('message', ['我该不该买这个猫条', '验收牌IGS猫条值得买吗？',
                                     '对比当前粮和验收牌IGS猫条', '拿验收牌IGS猫条和当前粮对比一下'])
def test_unknown_food_never_substitutes_current_food(monkeypatch, message):
    from services import miniprogram_chat_service as chat
    from services import guardian_health_flow_service as flow
    from unittest.mock import Mock
    state = chat._default_state('conversation')
    ctx = {'current_food': {'formula_id': 2, 'product_name': '已知粮'}, 'pet_profile': {}}
    monkeypatch.setattr(chat, '_build_context', lambda *args: ctx)
    monkeypatch.setattr(chat, '_food_analysis_by_formula', lambda fid: {'product': {'formula_id': fid}})
    catalog = Mock(return_value={'items': [{'product_name': '已知粮', 'formula_id': 2, 'catalog_key': 'formula:2'}]})
    monkeypatch.setattr(flow, 'list_standardized_product_options', catalog)
    decision = chat._recognize_guardian_turn(message, state)
    result = chat._handle_context_message('user', {'pet_id': 'cat'}, message, state, 'turn', intent_result=decision)
    assert result['result']['ingredient_guide']['stage'] == 'catalog'
    assert len(result['interaction']['options']) == 3
    assert '可核验' in result['reply'] and 'health_analysis' not in result['result']
    assert state['slots'][guide.SLOT]['original_question'] == message
    monkeypatch.setattr(guide, 'generate', lambda g, q, text, context: {
        'answer': '根据你提供的原料片段，只能初步说明蛋白来源。'})
    next_reply = guide.advance(state['slots'][guide.SLOT], '只知道鸡肉和兔肉，先帮我分析下', {}, 'next', allow_handoff=True)
    assert '原料片段' in next_reply['reply']
