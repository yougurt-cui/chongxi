from unittest.mock import patch
from services import miniprogram_chat_service as chat

EP42 = {'brand': '皇家', 'product_name': 'EP42', 'formula_id': 289, 'product_id': 143}


def prepare(candidates, current=None):
    state = chat._default_state('test')
    state['primary_intent'] = 'food_analysis'
    state['slots']['catalog_key'] = 'formula:344'
    context = {'current_food': dict(current or {'brand': '皇家', 'product_name': 'EP42'}),
               'diet_history': []}
    with patch.object(chat, 'list_standard_product_candidates', return_value=candidates) as lookup, \
            patch.object(chat, '_food_analysis_by_formula', return_value={'portrait': 'loaded'}) as load:
        chat._prepare_saved_food_analysis('帮我分析一下当前吃的粮', state, context)
    return state, context, lookup, load


def test_name_only_profile_loads_exact_unique_formula_and_keeps_current_role():
    state, context, _, load = prepare([EP42])
    assert context['current_food']['formula_id'] == 289
    assert context['food_analysis_scope'] == 'current'
    assert context['food_analysis'] == {'portrait': 'loaded'}
    assert state['slots']['catalog_key'] == 'formula:289'
    load.assert_called_once_with(289, 289)


def test_other_brand_and_substring_matches_never_supply_a_formula():
    for candidate in [{**EP42, 'brand': '其他品牌'}, {**EP42, 'product_name': 'EP42 Plus'}]:
        state, context, _, load = prepare([candidate])
        assert not context['current_food'].get('formula_id')
        assert 'food_analysis_scope' not in context
        assert 'catalog_key' not in state['slots']
        load.assert_not_called()


def test_multiple_versions_and_missing_food_go_to_existing_choice_flow():
    for candidates in [[], [EP42, {**EP42, 'formula_id': 290}]]:
        state, context, _, load = prepare(candidates)
        assert state['slots']['food_name'] == '皇家 EP42'
        assert 'food_analysis_scope' not in context
        load.assert_not_called()


def test_existing_formula_id_is_never_replaced_by_name_matching():
    _, context, lookup, load = prepare([EP42], {**EP42, 'formula_id': 290})
    lookup.assert_not_called()
    load.assert_called_once_with(290, 290)
    assert context['current_food']['formula_id'] == 290


def test_product_id_conflict_is_not_silently_rebound():
    _, context, _, load = prepare([EP42], {'brand': '皇家', 'product_name': 'EP42', 'product_id': 999})
    assert not context['current_food'].get('formula_id')
    load.assert_not_called()
