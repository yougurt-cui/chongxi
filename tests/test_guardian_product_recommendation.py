from copy import deepcopy
from unittest.mock import MagicMock, patch
import pytest
from services import guardian_product_recommendation as rec
from services import miniprogram_chat_service as chat

PET = {'animal_type': 'cat', 'breed': '布偶猫', 'age_months': 24, 'id': 'pet'}
STAGES = [dict(species_code='cat', stage_code=c, status='启用', age_min_months_inclusive=low,
               age_max_months_exclusive=high) for c, low, high in
          [('adult', 12, 84), ('kitten', 0, 12), ('senior', 84, None), ('all_stages', None, None), ('unspecified', None, None)]]
DICTS = {'dict_life_stage': STAGES,
         'dict_breed': [dict(breed_code='ragdoll', species_code='cat', breed_name='布偶猫', status='启用')],
         'dict_user_need': [dict(need_code='digestive_sensitivity', need_name='肠胃敏感', aliases_pipe='敏感肠胃')]}


def row(n=1):
    source = {'source_key': str(n), 'source_provider': 'deepseek', 'source_model': 'test',
              'recommendation': {'recommendation_reason': f'产品{n}的原始功能概述', 'review_status': 'pending'},
              'original_source': {'original_condition': '成年猫'}}
    return dict(id=n, product_key=f'key{n}', species_code='cat', breed_code='all_cat', life_stage_code='adult',
                need_code='coat_health', brand='品牌', product_name=f'产品{n}', entity_level='product',
                review_status='pending', verification_status='pending', sources=[source])


def recommend(rows, pet=None, slots=None):
    pet = pet or PET
    with patch.object(rec, 'load_material', return_value=({**pet, 'breed_code': 'ragdoll'}, DICTS, rows)):
        return rec.recommend(pet, slots or {'food_goal': 'coat_health'})


def test_source_fields_without_formula_or_score_invention():
    result = recommend([row()]); item = result['items'][0]
    assert result['status'] == 'ready' and result['version'] == 1
    assert item['function_summary'] == '产品1的原始功能概述'
    assert item['brand'] == '品牌' and item['product_name'] == '产品1'
    assert item['sources'][0]['source_key'] == '1'
    assert not {'formula_id', 'catalog_key', 'fit_score'} & item.keys()
    assert '尚未专业核验' in rec.reply_text(result)


@pytest.mark.parametrize('field,value', [('species_code', 'dog'), ('breed_code', 'british_shorthair'),
    ('life_stage_code', 'kitten'), ('review_status', 'rejected'), ('verification_status', 'rejected')])
def test_incompatible_or_rejected_rows_excluded(field, value):
    candidate = row(); candidate[field] = value
    assert not recommend([candidate])['items']


def test_rejected_source_not_merged():
    candidate = row(); bad = deepcopy(candidate['sources'][0]); bad.update(source_key='bad')
    bad['recommendation'].update(review_status='rejected', recommendation_reason='已拒绝的结论')
    candidate['sources'].append(bad)
    assert recommend([candidate])['items'][0]['function_summary'] == '产品1的原始功能概述'


@pytest.mark.parametrize('condition', ['仅限1–2岁', '成年猫；确诊肾病', '满3岁', '妊娠猫'])
def test_original_restrictions_survive_broad_age_tag(condition):
    candidate = row(); candidate['life_stage_code'] = 'all_stages'
    candidate['sources'][0]['original_source']['original_condition'] = condition
    assert not recommend([candidate], dict(PET, age_months=6))['items']


def test_unspecified_is_not_all_stages_and_unknown_age_is_optional():
    candidate = row(); candidate['life_stage_code'] = 'unspecified'
    assert not recommend([candidate], dict(PET, age_months=3))['items']
    candidate['sources'][0]['original_source']['original_condition'] = ''
    item = recommend([candidate], dict(PET, age_months=None))['items'][0]
    assert '未指明年龄不代表全阶段适用' in item['cautions']
    assert '未填写年龄，尚未确认年龄适配' in item['cautions']


def test_brand_only_community_is_truthful():
    candidate = row(); candidate.update(entity_level='brand', product_name='未明确')
    candidate['sources'][0]['source_provider'] = 'community_screenshot'
    result = recommend([candidate]); item = result['items'][0]
    assert item['source_label'] == '社区资料'
    assert '来源仅推荐品牌' in item['cautions'][0]
    assert '具体产品未明确' in rec.reply_text(result)


def test_dedupe_and_pagination_exclude_products_across_rows():
    rows = [row(i) for i in range(1, 6)]
    duplicate = deepcopy(rows[0]); duplicate['id'] = 99; rows.append(duplicate)
    first = recommend(rows)
    assert len(first['items']) == 3 and first['has_more']
    ids = [i['product_key'] for i in first['items']]
    second = recommend(rows, slots={'food_goal': 'coat_health', '_recommendation_exclude_ids': ids})
    assert len(second['items']) == 2 and not second['has_more']
    assert not set(ids) & {i['product_key'] for i in second['items']}


def test_history_current_food_and_brand_preferences():
    with patch.object(rec, 'load_material', return_value=(PET, DICTS, [row(1), row(2), row(3)])):
        result = rec.recommend(dict(PET, food_brand='品牌', food_product='产品1'),
                               {'food_goal': 'coat_health'}, [{'brand': '品牌', 'product': '产品2'}])
    assert [r['product_name'] for r in result['items']] == ['产品3']
    assert not recommend([row()], slots={'preferred_brand': '其他品牌'})['items']


@pytest.mark.parametrize('pet,slots', [(dict(PET, diseases=['肾病']), {}), (dict(PET, allergies=['鸡肉']), {}),
    (PET, {'food_health_state': 'vomiting', 'food_health_stage': 'active'}),
    (PET, {'budget_per_jin': 20}), (PET, {'origin_type': 'domestic'})])
def test_unverifiable_restrictions_do_not_claim_match(pet, slots):
    with patch.object(rec, 'load_material') as load:
        assert rec.recommend(pet, slots)['items'] == []; load.assert_not_called()


def test_read_failure_explicit():
    with patch.object(rec, 'load_material', side_effect=RuntimeError('db unavailable')):
        assert rec.recommend(PET, {})['status'] == 'data_unavailable'


def test_read_only_parameterized_query_and_sensitivity_not_soft_stool():
    conn = MagicMock(); cur = conn.__enter__.return_value.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = [DICTS['dict_breed'], STAGES, DICTS['dict_user_need'], []]
    with patch.object(rec, '_connect', return_value=conn):
        rec.load_material(PET, {'food_goal': 'soft_stool', 'target_requirement': '敏感肠胃'})
    sql, params = cur.execute.call_args.args
    assert 'digestive_sensitivity' in params and 'soft_stool' not in params
    assert 'ragdoll' in params and 'unknown_cat' in params
    assert all(c.args[0].lstrip().startswith('SELECT') for c in cur.execute.call_args_list)


def test_chat_completion_refresh_and_stale_token():
    state = chat._default_state('c'); state['primary_intent'] = 'food_switch'
    state['slots'].update(food_goal='coat_health', food_health_state='none', food_health_stage='none')
    context = {'pet_profile': PET, 'diet_history': [], 'food_list': []}; conv = {'id': 'c', 'pet_id': 'pet'}
    outputs = [recommend([row(i) for i in range(1, 6)]), recommend([row(4), row(5)])]
    with patch.object(chat, '_build_context', return_value=context), patch.object(rec, 'recommend', side_effect=outputs):
        first = chat._handle_context_message('u', conv, '', state, interaction={'slot': 'food_goal', 'value': 'coat_health'})
        result = first['result']['product_recommendations']
        assert first['result']['food_candidates'] == [] and state['slots']['_food_flow']['candidates'] == []
        assert '产品1' in first['reply'] and '结构参考分' not in first['reply']
        token = result['refresh_token']
        second = chat._handle_context_message('u', conv, '', state,
                    interaction={'slot': 'product_recommendation_refresh', 'value': token})
        assert len(second['result']['product_recommendations']['items']) == 2
        assert state['slots']['_recommendation_exclude_ids'] == ['key1', 'key2', 'key3']
        with pytest.raises(ValueError):
            chat._handle_context_message('u', conv, '', state,
                interaction={'slot': 'product_recommendation_refresh', 'value': token})
