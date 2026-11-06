from unittest.mock import patch
from services.guardian_food_flow_service import resolve_target
from services import guardian_health_flow_service as flow


def option(fid=334, name='IGS小金条', brand='添赐力'):
    return {'formula_id': fid, 'catalog_key': f'formula:{fid}', 'brand': brand,
            'product_name': name, 'standard_product_name': name, 'label': brand + ' ' + name}


def test_chinese_brand_product_shorthand_resolves():
    row = option()
    assert resolve_target('添赐力小金条', [row]) == [row]
    assert resolve_target('添赐力 IGS小金条', [row]) == [row]
    assert not resolve_target('其他牌小金条', [row])
    assert not resolve_target('添赐力ABC小金条', [row])


def test_versions_and_flavours_remain_ambiguous():
    rows = [option(), option(335, 'IGS小金条2.0'), option(336, 'IGS小金条鸡肉兔肉')]
    assert len(resolve_target('添赐力小金条', rows)) == 3
    assert resolve_target('添赐力小金条2.0', rows) == [rows[1]]
    assert resolve_target('添赐力小金条鸡肉兔肉', rows) == [rows[2]]
    assert not resolve_target('皇家24', [option(1, 'T24', '皇家')])


def test_direct_question_loads_named_formula():
    row = option()
    state, context = {'slots': {}}, {}
    with patch.object(flow, 'list_standardized_product_options', side_effect=[{'items': []}, {'items': [row]}]), \
         patch.object(flow, 'resolve_target', wraps=resolve_target):
        result = flow.prepare_named_analysis('帮我分析一下添赐力小金条', state, context,
            lambda fid: {'product': {'formula_id': fid}, 'product_portrait': {'result_stage': 'provisional', 'result_id': '27'}})
    assert result is None
    assert state['slots']['catalog_key'] == 'formula:334'
    assert context['named_food_analyses'][0]['food_analysis']['product_portrait']['result_id'] == '27'


def test_brand_category_questions_offer_only_that_brands_products():
    rows = [option(4, '荒野猎兔', '蓝氏'), option(5, '猎鸟乳鸽全价猫粮', '蓝氏'),
            option(6, '猎宴火鸡全价猫粮', '蓝氏'), option(290, '猎鸟乳鸽2.0', '蓝氏'), option()]
    for question in ['分析一下蓝氏的产品', '帮我分析一下蓝氏猫粮']:
        state, context = {'slots': {}}, {}
        with patch.object(flow, 'list_standardized_product_options', side_effect=[{'items': []}, {'items': rows}]):
            result = flow.prepare_named_analysis(question, state, context, lambda fid: None)
        assert result['interaction']['slot'] == flow.CHOICE_SLOT
        assert len(result['interaction']['options']) == 4
        assert all(x['label'].startswith('蓝氏') for x in result['interaction']['options'])
    assert resolve_target('蓝氏猎鸟乳鸽2.0', rows) == [rows[3]]
    assert not resolve_target('未知牌猫粮', rows)
