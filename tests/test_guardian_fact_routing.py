from contextlib import ExitStack
from unittest.mock import MagicMock, patch
from services import guardian_conversation_context as ctx, miniprogram_chat_service as chat
from tests.test_guardian_task_recognizer import health_memory


def test_fact_turn_reaches_context_with_named_evidence_without_recommendation():
    memory = health_memory(); state = chat._default_state('c'); saved = []
    conv = dict(id='c', pet_id='p', profile_species='cat', profile_age=24)
    def save(**kw):
        row = dict(id='saved' + str(len(saved)), **kw); saved.append(row); return row
    def model(system, payload, **kwargs):
        if 'active_context' in payload:
            return {'relation': 'continue', 'task': 'business', 'dialogue_act': 'provide_fact',
                    'facts_to_merge': {'new_food_name': '卫仕LA20'}, 'business_intent': 'food_switch'}
        assert payload['facts_to_merge'] == {'new_food_name': '卫仕LA20'}
        assert payload['product_evidence'][0]['analysis']['product']['formula_id'] == 20
        assert any(e['id'] == 'answer' for e in payload['evidence'])
        return {'conclusion': '换后的粮是卫仕LA20，继续结合便臭核对配方。',
                'reasons': [{'text': '你补充了换后的粮款。', 'source_ids': ['saved0']}],
                'missing': [], 'questions': ['纤维来源如何影响发酵？', '蛋白消化和便臭有什么关系？', '如何核对换粮后的变化？']}
    with ExitStack() as stack:
        def mock(obj, name, **kw): return stack.enter_context(patch.object(obj, name, **kw))
        mock(chat, '_get_conversation', return_value=conv)
        mock(chat, '_load_state', return_value=state)
        mock(chat, '_save_state'); mock(chat, '_save_message', side_effect=save)
        mock(chat, '_connect', return_value=MagicMock())
        mock(ctx, 'load', return_value=memory); mock(ctx, 'contextual_questions')
        mock(ctx, '_model', side_effect=model)
        mock(chat, '_resolve_scene', return_value=(None, [], None))
        mock(chat, '_build_context', return_value={})
        mock(chat, '_recognize_guardian_turn', side_effect=AssertionError('legacy routing forbidden'))
        mock(chat.health_flow, 'list_standardized_product_options', return_value={'items': [
            {'formula_id': 20, 'catalog_key': 'formula:20', 'brand': '卫仕', 'product_name': 'LA20', 'label': '卫仕 LA20'}]})
        mock(chat, '_food_analysis_by_formula', return_value={'product': {'formula_id': 20}, 'product_portrait': {}})
        reply = chat.handle_message('u', 'c', {'message': '换了卫仕LA20'})
    assert reply['message']['result']['context_answer']['status'] == 'answered'
    assert state['current_step'] is None
    assert memory['topics'][0]['facts']['new_food_name']['source_id'] == 'saved0'
    assert 'catalog_key' not in state['slots']


def test_validated_business_intent_bypasses_second_model():
    with patch.object(chat, '_extract_intent', side_effect=AssertionError('second intent model forbidden')):
        for intent in ['food_analysis', 'food_switch', 'reminder']:
            turn, _ = chat._recognize_guardian_turn('卫仕LA20怎么样', chat._default_state('c'),
                {'task': 'business', 'business_intent': intent, 'facts_to_merge': {'current_food_name': '卫仕LA20'}})
            assert turn['primary_intent'] == intent


def test_ambiguous_named_product_keeps_candidates_and_does_not_choose():
    rows = [{'formula_id': n, 'catalog_key': f'formula:{n}', 'brand': '卫仕',
             'product_name': name, 'label': '卫仕 ' + name} for n, name in [(20, 'LA20'), (21, 'LA20鸡肉')]]
    with patch.object(chat.health_flow, 'list_standardized_product_options', return_value={'items': rows}), \
         patch.object(chat, '_food_analysis_by_formula') as load:
        product = chat._context_named_product('卫仕LA20', 'new_food_name')
    assert product['identity_status'] == 'ambiguous' and len(product['candidates']) == 2
    load.assert_not_called()


def test_named_analysis_request_uses_query_without_claiming_it_is_current_food():
    with patch.object(chat, '_extract_intent', side_effect=AssertionError('no reclassification')):
        turn, _ = chat._recognize_guardian_turn('卫仕LA20怎么样', chat._default_state('c'),
            {'task': 'business', 'business_intent': 'food_analysis', 'product_query': '卫仕LA20'})
    assert turn['primary_intent'] == 'food_analysis'
    assert turn['slots'] == {'food_name': '卫仕LA20'}
