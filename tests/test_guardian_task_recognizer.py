from copy import deepcopy
from unittest.mock import Mock

import pytest
from services import guardian_conversation_context as ctx
from services.guardian_task_recognizer import recognize


def memory():
    m = ctx.empty('u', {'id': 'c', 'pet_id': 'p'})
    for i, text, role, result in [
        (1, '分析这两款粮', 'user', {}),
        (2, '原配方对比', 'assistant', {'health_analysis': {'subjects': [
            {'formula_id': 343, 'label': '卫仕'}, {'formula_id': 342, 'label': '皇家'}]}}),
        (3, '另外聊玩具', 'user', {}), (4, '玩具知识', 'assistant', {}),
    ]:
        ctx.remember(m, {'id': str(i), 'role': role, 'content': text, 'result': result})
    m['source_id'] = '2'
    return m


@pytest.mark.parametrize('question,task,action', [
    ('这两款粮的配方差异，日常喂养时怎么理解？', 'explain', 'continue'),
    ('这两款哪个区别值得先关注？', 'explain', 'continue'),
    ('再看看这两款的纤维原料', 'supplement', 'continue'),
    ('重新完整比较这两款粮', 'compare', 'compare'),
])
def test_task_is_semantic_and_source_object_is_preserved(question, task, action):
    m = memory(); original = deepcopy(m)
    model = Mock(return_value={'relation': 'continue', 'task': task, 'target': '原回答中的粮款'})
    d = recognize(question, m, {'primary_intent': 'food_analysis'}, model)
    assert d['action'] == action and d['task'] == task
    assert d['source_ids'] == ['2'] and d['product_ids'] == [343, 342]
    assert d['topic_ids'] == ['topic:1']
    assert m == original
    assert model.call_count == 1
    assert model.call_args.args[1]['workflow']['intent'] == 'food_analysis'


@pytest.mark.parametrize('value', [
    {'relation': 'continue', 'task': 'compare', 'product_ids': [999]},
    {'relation': 'continue', 'task': 'explain', 'topic_ids': ['foreign']},
    {'relation': 'continue', 'task': 'explain', 'source_ids': ['foreign']},
    {'relation': 'continue', 'task': 'bogus'},
    {'relation': 'bogus', 'task': 'explain'},
    {'relation': 'continue', 'task': 'compare', 'product_ids': [True]},
    {'relation': 'continue', 'task': 'explain', 'source_ids': '2'},
])
def test_untrusted_classification_never_executes_business(value):
    assert recognize('这两款粮的差异', memory(), {}, Mock(return_value=value))['action'] == 'unavailable'


def test_failures_do_not_fall_back_to_keyword_comparison():
    d = recognize('这两款粮的配方差异，日常喂养时怎么理解？', memory(), {}, Mock(side_effect=TimeoutError))
    assert d['action'] == 'unavailable'


def test_business_continuation_preserves_workflow_and_new_task_is_distinct():
    m = memory(); m['source_id'] = None
    d = recognize('预算每斤50', m, {'primary_intent': 'food_switch', 'current_step': 'budget'},
                  Mock(return_value={'relation': 'continue', 'task': 'business'}))
    assert d['action'] == 'workflow'
    d = recognize('另外帮我推荐新粮', m, {}, Mock(return_value={'relation': 'new', 'task': 'business'}))
    assert d['action'] == 'new'


def test_missing_comparison_target_clarifies_instead_of_guessing():
    m = memory(); m['products'] = m['products'][:1]
    d = recognize('比较这两款', m, {}, Mock(return_value={'relation': 'continue', 'task': 'compare'}))
    assert d['action'] == 'clarify'


def test_first_turn_keeps_existing_entry():
    model = Mock()
    assert recognize('分析猫粮', {'events': []}, {}, model) is None
    model.assert_not_called()


@pytest.mark.parametrize('question', [
    '帮我分析一下当前吃的粮和历史吃的粮',
    '帮我分析一下历史粮和现在吃的粮',
    '对比一下当前粮和历史粮',
])
def test_explicit_product_request_cannot_be_rewritten_as_symptom_attribution(question):
    m = memory(); m['products'] = []; m['source_id'] = None
    model = Mock(return_value={'relation': 'continue', 'task': 'synthesize', 'target': '便臭归因'})
    d = recognize(question, m, {}, model)
    assert d['action'] == 'product_analysis' and d['relation'] == 'new'
    assert d['object_scope'] == 'saved_diet'
    assert d['source_ids'] == [] and d['topic_ids'] == []
    assert model.call_args.args[1]['request_contract']['task'] == 'product_analysis'


def test_explicit_saved_product_request_survives_model_failure_and_first_turn():
    model = Mock(side_effect=TimeoutError)
    q = '帮我分析一下当前吃的粮和历史吃的粮'
    assert recognize(q, memory(), {}, model)['action'] == 'product_analysis'
    assert recognize(q, {'events': []}, {}, model)['action'] == 'product_analysis'


@pytest.mark.parametrize('question,task', [
    ('结合便臭分析当前粮和历史粮的影响', 'synthesize'),
    ('帮我分析一下新粮和历史粮的纤维组成', 'supplement'),
    ('当前粮和历史粮的差异日常怎么理解', 'explain'),
])
def test_contract_does_not_capture_explanations_supplements_or_explicit_attribution(question, task):
    m = memory()
    model = Mock(return_value={'relation': 'continue', 'task': task, 'source_ids': ['2']})
    d = recognize(question, m, {}, model)
    assert d['task'] == task and 'constraint' not in d
    assert d['object_scope'] == 'saved_diet'
