from unittest.mock import patch
import pymysql
from services import guardian_task_knowledge as task
from test_guardian_task_knowledge import dim, pair, rule


def test_market_tags_hide_unmapped_and_only_show_matching_observations():
    products = [dict(id='a', formula_id=343, status='ready'), dict(id='b', formula_id=342, status='ready')]
    rules = [rule(1,'protein_quality','protein_quality','increase',True),
             rule(2,'fiber_support','fiber','increase',True),
             rule(3,'protein_pressure','protein','decrease',True)]
    dims = [dim('fiber',1,3,5), dim('protein',.7,.2,kind='higher_is_burden')]
    with patch.object(task,'load_rules',return_value=rules), patch.object(task,'switch_health_rows',return_value=[]), patch.object(task,'load_market_advantages',return_value={343:['动物蛋白','脂肪','功能性营养'],342:['纤维','适口性']}):
        result = task.nutrition_pairs([pair(dims)],'cat',products)['pairs'][0]
    a,b = result['products']
    assert [s['title'] for s in a['strengths']] == ['动物蛋白']
    assert [s['title'] for s in b['strengths']] == ['纤维']
    assert a['strengths'][0]['rule_id'] == 1
    assert b['strengths'][0]['rule_id'] == 2
    assert {o['rule_id'] for o in a['observations']} == {1}
    assert {o['rule_id'] for o in b['observations']} == {2}
    assert all(s['title'] != 'protein' for s in b['strengths'])


def test_shared_tags_and_market_failure_do_not_reuse_pairwise_advantages():
    rules = [rule(1,'fiber_support','fiber','increase',True)]
    assert task.market_strengths(['纤维'],rules,{'纤维'})[0]['shared']
    assert task.market_strengths(['适口性'],rules,set()) == []
    with patch.object(task,'load_rules',return_value=rules), patch.object(task,'switch_health_rows',return_value=[]), patch.object(task,'load_market_advantages',side_effect=pymysql.OperationalError('offline')):
        r=task.nutrition_pairs([pair([dim('fiber',1,3,5)])],'cat')['pairs'][0]
    assert all(not p['strengths'] and p['market_status']=='unavailable' for p in r['products'])
    assert not r['products'][1]['observations']


def test_advantage_observations_deduplicate_text():
    products = [dict(id='a', formula_id=1, status='ready')]
    rules = [rule(1,'fiber_support','fiber','increase',True), rule(2,'prebiotic_support','prebiotic','increase',True)]
    rules[0]['owner_observation'] = '便便形态、排便规律'
    rules[1]['owner_observation'] = '便便形态，排便规律。'
    with patch.object(task,'load_rules',return_value=rules), patch.object(task,'switch_health_rows',return_value=[]), patch.object(task,'load_market_advantages',return_value={1:['纤维','益生元']}):
        result=task.nutrition_pairs([pair([])],'cat',products)['pairs'][0]['products'][0]
    assert len(result['strengths']) == 2
    assert len(result['observations']) == 1
