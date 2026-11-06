from copy import deepcopy
from unittest.mock import patch

from services import miniprogram_chat_service as chat


def old_state():
    state = chat._default_state('c1')
    state.update(primary_intent='food_analysis')
    state['slots'].update(food_name='皇家 DR25', catalog_key='formula:12')
    state['slots'][chat.health_flow.STATE_KEY] = {'analysis_id': 'old', 'pet_id': 'p1',
        'stage': 'pending', 'subjects': [{'label': '皇家 DR25', 'formula_id': 12}]}
    return state


def test_upload_replaces_snapshot_and_followup_uses_uploaded_product():
    state = old_state()
    submission = {'id': 's1', 'formula_id': 335, 'recognized_brand': '麦富迪',
        'recognized_product_name': '鸡肉蔓越莓', 'result_available': True,
        'calculation_result': {'portrait': {'formula_id': 335, 'summary': '本次上传画像'},
                               'result_stage': 'provisional', 'result_id': '29'}}
    context = {'food_submission': submission, 'current_food': {'formula_id': 12},
               'food_health_analysis': deepcopy(state['slots'][chat.health_flow.STATE_KEY])}
    with patch.object(chat, '_build_context', return_value=context), \
         patch.object(chat, '_food_analysis_by_formula', return_value=None), \
         patch('services.formula_calculation_service.submission_reply',
               return_value={'reply': '上传结果', 'result': {}}) as builder, \
         patch('services.guardian_report_service.attach_report') as report:
        chat._handle_context_message('u1', {'pet_id': 'p1'}, '', state,
            attachments=[{'type': 'food_submission', 'submission_id': 's1'}])
    builder.assert_called_once_with(submission, None)
    assert len(report.call_args.args[2]) == 1
    assert report.call_args.args[2][0]['roles'] == ['uploaded']
    snapshot = state['slots'][chat.health_flow.STATE_KEY]
    assert snapshot['subjects'][0]['formula_id'] == 335
    assert snapshot['subjects'][0]['result_stage'] == 'provisional'
    assert snapshot['analysis_id'] != 'old'
    for question in ['这张图的产品怎么样', '这款粮怎么样', '刚上传的产品有什么缺点']:
        assert chat._uploaded_product_followup(question, state)
        assert chat._recognize_guardian_turn(question, state)[0]['primary_intent'] == 'food_analysis'
    followup_context = {'food_health_analysis': deepcopy(snapshot), 'current_food': {'formula_id': 12}}
    with patch.object(chat, '_build_context', return_value=followup_context), \
         patch.object(chat, '_generate_answer', return_value=('麦富迪初算待核对', 'test')) as generate, \
         patch.object(chat.health_flow, 'prepare_named_analysis') as named:
        reply = chat._handle_context_message('u1', {'pet_id': 'p1'}, '这款粮怎么样', state,
            intent_result=({'primary_intent': 'food_analysis', 'slots': {}}, None))
    assert reply['reply'] == '麦富迪初算待核对'
    assert generate.call_args.args[2]['food_health_analysis']['subjects'][0]['formula_id'] == 335
    named.assert_not_called()
    assert not chat._uploaded_product_followup('分析当前吃的粮', state)
    assert not chat._uploaded_product_followup('分析皇家DR25', state)


def test_processing_upload_invalidates_old_analysis_and_buttons():
    state = old_state()
    state['slots'][chat.health_flow.RECOMMENDATION_KEY] = {'analysis_id': 'old'}
    context = {'food_submission': {'id': 's2', 'recognition_status': 'processing'}, 'current_food': {}}
    with patch.object(chat, '_build_context', return_value=context):
        reply = chat._handle_context_message('u1', {'pet_id': 'p1'}, '', state,
            attachments=[{'type': 'food_submission', 'submission_id': 's2'}])
    assert reply['response_type'] == 'processing'
    assert chat.health_flow.STATE_KEY not in state['slots']
    assert chat.health_flow.RECOMMENDATION_KEY not in state['slots']
    assert 'catalog_key' not in state['slots']
    assert state['current_step'] is None


def test_plain_upload_does_not_load_or_render_current_food_comparison():
    from services.formula_calculation_service import submission_reply
    submission = {'id': 's1', 'recognized_product_name': '上传粮',
        'calculation_result': {'portrait': {'summary': '本款配方特点'},
                               'result_stage': 'provisional', 'result_id': 'r1'}}
    with patch('services.guardian_product_portrait_service.load_product_portraits') as load, \
         patch('services.guardian_product_portrait_service.compare_portraits') as compare:
        reply = submission_reply(submission)
    load.assert_not_called()
    compare.assert_not_called()
    assert reply['result']['portrait_comparison'] is None
    assert reply['result']['comparison_current_portrait'] is None
    assert '比较' not in reply['reply']
    assert '尚未记录' not in reply['reply']


def test_explicit_comparison_reloads_uploaded_food_and_compares_to_current():
    state = old_state()
    state['slots'][chat.health_flow.STATE_KEY].update(scope='uploaded', submission_id='s1')
    state['slots']['food_name'] = '麦富迪 鸡肉蔓越莓'
    state['slots']['catalog_key'] = 'formula:335'
    submission = {'id': 's1', 'formula_id': 335, 'recognized_product_name': '鸡肉蔓越莓',
        'result_available': True, 'calculation_result': {'portrait': {'formula_id': 335},
        'result_stage': 'provisional', 'result_id': 'r1'}}
    context = {'food_submission': None, 'current_food': {'formula_id': 12},
               'food_health_analysis': deepcopy(state['slots'][chat.health_flow.STATE_KEY])}
    question = '和当前粮比怎么样'
    assert chat._uploaded_current_comparison(question, state)
    assert not chat._uploaded_current_comparison('这款怎么样', state)
    assert not chat._uploaded_current_comparison('分析当前吃的粮', state)
    assert chat._uploaded_current_comparison('', state, {'slot': 'food_comparison', 'value': 'current'})
    with patch.object(chat, '_build_context', return_value=context), \
         patch.object(chat, 'get_food_submission', return_value={'item': submission}) as load, \
         patch('services.formula_calculation_service.submission_reply', return_value={
             'reply': '明确对比', 'result': {'comparison_current_portrait': {'formula_id': 12}}}) as reply_builder, \
         patch('services.guardian_report_service.attach_report') as report, \
         patch.object(chat, '_prepare_saved_food_analysis') as saved:
        reply = chat._handle_context_message('u1', {'pet_id': 'p1'}, question, state,
            intent_result=({'primary_intent': 'food_analysis', 'slots': {}}, None))
    load.assert_called_once_with('u1', 's1')
    reply_builder.assert_called_once_with(submission, 12)
    saved.assert_not_called()
    subjects = report.call_args.args[2]
    assert [s['formula_id'] for s in subjects] == [12, 335]
    assert report.call_args.kwargs['scene'] == 'product_comparison'
    assert reply['reply'] == '明确对比'
