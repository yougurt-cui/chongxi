from unittest.mock import patch
import pymysql

from services import guardian_task_knowledge as task
from services import guardian_health_flow_service as flow
from services import guardian_report_service as report
from test_guardian_task_knowledge import rule, pair
from test_guardian_health_analysis import portrait, knowledge
from test_guardian_report import profile, EMPTY_COMMUNITY


def product(**overrides):
    return dict(id='a', formula_id=1, label='粮1', status='ready') | overrides


def test_single_uses_exactly_same_advantages_and_observations_as_comparison():
    p = product()
    rules = [rule(1, 'fiber_support', 'fiber', 'increase', True)]
    with patch.object(task, 'load_rules', return_value=rules), \
         patch.object(task, 'load_market_advantages', return_value={1: ['纤维', '适口性']}), \
         patch.object(task, 'switch_health_rows', return_value=[]) as health:
        single = task.nutrition_products([p])
        health.assert_not_called()
        comparison = task.nutrition_pairs([pair([])], 'cat', [p])
    a, b = single['products'][0], comparison['pairs'][0]['products'][0]
    assert a['strengths'] == b['strengths']
    assert a['observations'] == b['observations']
    assert [s['title'] for s in a['strengths']] == ['纤维']
    assert single['pairs'] == []


def test_single_observations_deduplicate_and_do_not_borrow_unmatched_rules():
    rules = [rule(1, 'fiber_support', 'fiber', 'increase', True),
             rule(2, 'prebiotic_support', 'prebiotic', 'increase', True),
             rule(3, 'fat_pressure', 'fat', 'decrease', True)]
    rules[0]['owner_observation'] = '便便形态、排便规律'
    rules[1]['owner_observation'] = '便便形态，排便规律。'
    with patch.object(task, 'load_rules', return_value=rules), \
         patch.object(task, 'load_market_advantages', return_value={1: ['纤维', '益生元']}):
        p = task.nutrition_products([product()])['products'][0]
    assert len(p['strengths']) == 2 and len(p['observations']) == 1
    assert all(s['rule_id'] != 3 for s in p['strengths'] + p['observations'])


def test_single_missing_rules_does_not_fabricate_content():
    with patch.object(task, 'load_rules', side_effect=pymysql.OperationalError('offline')):
        n = task.nutrition_products([product()])
    assert n['status'] == 'unavailable' and n['products'] == []
    assert '暂时无法读取' in task.single_nutrition_text(n)


def test_provisional_identity_cannot_borrow_market_advantages():
    with patch.object(task, 'load_rules', return_value=[rule(1, 'fiber_support', 'fiber', 'increase', True)]), \
         patch.object(task, 'load_market_advantages', return_value={1: ['纤维']}):
        n = task.nutrition_products([product(status='provisional')])
    assert n['products'][0]['strengths'] == []
    assert n['products'][0]['observations'] == []
    assert n['products'][0]['market_status'] == 'unavailable'


def test_single_without_matching_advantages_has_explicit_empty_sections():
    with patch.object(task, 'load_rules', return_value=[]), \
         patch.object(task, 'load_market_advantages', return_value={1: ['适口性']}):
        n = task.nutrition_products([product()])
    text = task.single_nutrition_text(n)
    assert '营养优势' in text and '日常关注' in text
    assert '暂未匹配' in text and not n['products'][0]['observations']


def test_single_report_exposes_structured_nutrition_and_plain_text_before_confirmation():
    context = {'food_analysis_scope': 'current', 'pet_profile': {'animal_type': 'cat'},
               'current_food': {'formula_id': 1, 'product_name': '粮1'},
               'food_analysis': {'product': {'formula_id': 1}, 'product_portrait': portrait()}}
    with patch.object(flow, 'load_health_mappings', return_value=knowledge()), \
         patch.object(report, 'load_profiles', return_value={1: profile()}), \
         patch.object(report, 'community_evidence', return_value=EMPTY_COMMUNITY), \
         patch.object(report, 'get_chat_model_config', return_value={'api_key': ''}), \
         patch.object(task, 'load_rules', return_value=[rule(1, 'fiber_support', 'fiber', 'increase', True)]), \
         patch.object(task, 'load_market_advantages', return_value={1: ['纤维']}), \
         patch.object(task, 'switch_health_rows', side_effect=AssertionError('no switch query for single food')):
        result = flow.build_analysis_reply('u', {'pet_id': None}, {'slots': {}, 'conversation_id': 'c'}, context)
    nutrition = result['result']['guardian_report']['feeding']['task_nutrition']
    assert nutrition['mode'] == 'single' and nutrition['pairs'] == []
    assert len(nutrition['products']) == 1 and nutrition['products'][0]['strengths']
    assert '好在哪' in result['reply'] and '哪里需要留心' in result['reply']
    assert '**营养优势**' in result['reply'] and '**日常关注**' in result['reply']
    assert result['reply'].index('**日常关注**') < result['reply'].index('已经在吃这款粮了吗')
    assert result['result']['guardian_report']['feeding']['comparisons'] == []
