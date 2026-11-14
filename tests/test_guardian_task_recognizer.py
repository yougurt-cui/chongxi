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


def health_memory():
    m = ctx.empty('u', {'id': 'c', 'pet_id': 'p'})
    ctx.remember(m, {'id': 'symptom', 'role': 'user', 'content': '换粮后拉粑粑好臭', 'result': {}})
    ctx.remember(m, {'id': 'answer', 'role': 'assistant', 'content': '可以核对换粮、蛋白消化和纤维组成。',
                     'result': {'pipeline': 'health_knowledge', 'health_knowledge': {'groups': []}}})
    return m


@pytest.mark.parametrize('utterance,act,facts', [
    ('换了卫仕LA20', 'provide_fact', {'new_food_name': '卫仕LA20'}),
    ('卫仕LA20', 'answer_question', {'new_food_name': '卫仕LA20'}),
    ('不是LA20，是卫仕LA40', 'correct_fact', {'new_food_name': '卫仕LA40'}),
    ('已经三天了', 'provide_fact', {'duration': '三天'}),
])
def test_fact_updates_resume_health_task_even_if_model_says_business(utterance, act, facts):
    m = health_memory()
    model = Mock(return_value={'relation': 'continue', 'task': 'business',
                              'dialogue_act': act, 'facts_to_merge': facts, 'business_intent': 'food_switch'})
    d = recognize(utterance, m, {}, model)
    assert d['action'] == 'continue' and d['task'] == 'explain'
    assert d['business_intent'] is None and d['facts_to_merge'] == facts
    assert d['topic_ids'] == ['topic:symptom']
    active = model.call_args.args[1]['active_context']
    assert active['task'] == 'symptom_cause_analysis'
    assert active['user_statements'] == ['换粮后拉粑粑好臭']


def test_new_fact_does_not_start_recommendation_or_attach_to_unrelated_topic():
    d = recognize('换了卫仕LA20', memory(), {}, Mock(return_value={
        'relation': 'new', 'task': 'business', 'dialogue_act': 'provide_fact',
        'facts_to_merge': {'new_food_name': '卫仕LA20'}, 'business_intent': 'food_switch'}))
    assert d['action'] == 'clarify' and d['business_intent'] is None


def test_fact_corrections_are_scoped_persisted_and_replayed():
    m = health_memory()
    for i, text, act, name in [('f1', '换了卫仕LA20', 'provide_fact', '卫仕LA20'),
                                ('f2', '不是LA20，是卫仕LA40', 'correct_fact', '卫仕LA40')]:
        d = recognize(text, m, {}, Mock(return_value={
            'relation': 'continue', 'task': 'explain', 'dialogue_act': act,
            'source_ids': ['answer'], 'facts_to_merge': {'new_food_name': name}}))
        ctx.remember(m, {'id': i, 'role': 'user', 'content': text,
                         'result': {'discussion': ctx.discussion(d)}})
    fact = m['topics'][0]['facts']['new_food_name']
    assert fact == {'field': 'new_food_name', 'value': '卫仕LA40', 'source_id': 'f2'}
    model = Mock(return_value={'relation': 'continue', 'task': 'explain', 'source_ids': ['answer']})
    recognize('继续分析', m, {}, model)
    assert model.call_args.args[1]['active_context']['known_facts'] == [fact]
    assert 'facts' not in memory()['topics'][0]


@pytest.mark.parametrize('facts', [
    {'new_food_name': '皇家'}, {'diagnosis': '疾病'}, {'duration': 3},
])
def test_extracted_facts_must_be_supported_by_user_text(facts):
    d = recognize('换了卫仕LA20', health_memory(), {}, Mock(return_value={
        'relation': 'continue', 'task': 'explain', 'dialogue_act': 'provide_fact', 'facts_to_merge': facts}))
    assert d['action'] == 'unavailable'


def test_budget_answer_keeps_existing_questionnaire_and_explicit_requests_keep_their_intent():
    state = {'primary_intent': 'food_switch', 'current_step': 'budget'}
    d = recognize('每斤50元', health_memory(), state, Mock(return_value={
        'relation': 'continue', 'task': 'business', 'dialogue_act': 'answer_question',
        'business_intent': 'food_switch'}))
    assert d['action'] == 'workflow' and d['business_intent'] == 'food_switch'
    for question, intent in [('帮我推荐一款粮', 'food_switch'), ('卫仕LA20怎么样', 'food_analysis')]:
        d = recognize(question, health_memory(), {}, Mock(return_value={
            'relation': 'new', 'task': 'business', 'dialogue_act': 'request', 'business_intent': intent}))
        assert d['action'] == 'new' and d['business_intent'] == intent


def test_correction_of_current_food_removes_obsolete_new_food_value():
    m = health_memory()
    for i, text, act, facts in [('f1', '换了卫仕LA20', 'provide_fact', {'new_food_name': '卫仕LA20'}),
                               ('f2', '不是LA20，是卫仕LA40', 'correct_fact', {'current_food_name': '卫仕LA40'})]:
        ctx.remember(m, {'id': i, 'role': 'user', 'content': text, 'result': {'discussion': {
            'topic_ids': ['topic:symptom'], 'dialogue_act': act, 'facts_to_merge': facts}}})
    assert set(m['topics'][0]['facts']) == {'current_food_name'}
    assert m['topics'][0]['facts']['current_food_name']['value'] == '卫仕LA40'


def test_product_query_cannot_invent_identity_or_become_a_diet_fact():
    for query, action in [('卫仕LA20', 'new'), ('皇家', 'unavailable')]:
        d = recognize('卫仕LA20怎么样', health_memory(), {}, Mock(return_value={
            'relation': 'new', 'task': 'business', 'dialogue_act': 'request',
            'business_intent': 'food_analysis', 'product_query': query}))
        assert d['action'] == action
        if action == 'new':
            assert d['facts_to_merge'] == {} and d['product_query'] == '卫仕LA20'
