from unittest.mock import patch
from services import guardian_task_knowledge as task
from test_guardian_task_knowledge import dim,pair


def test_manifestations_only_from_sources_matching_displayed_metric():
    point=dict(id=1,issue_code='SOFT_STOOL',causal_chains=[dict(cause='高脂饮食',source_ids=[11,12],
        manifestations=['脂肪表现','其他指标表现'],nutrition_factors=[
            dict(factor='FAT_BURDEN',direction='increase',relation='脂肪关联'),
            dict(factor='PROTEIN_QUALITY',direction='decrease',relation='蛋白关联')])],
        detail_evidence=[dict(id=11,nutrition_factors=[dict(factor='FAT_BURDEN',direction='increase')],manifestations=['脂肪表现']),
                         dict(id=12,nutrition_factors=[dict(factor='PROTEIN_QUALITY',direction='decrease')],manifestations=['其他指标表现'])])
    links=task.switch_health_links([point],'fat','increase')
    assert len(links)==1 and links[0]['manifestations']==['脂肪表现']
    assert links[0]['detail_source_ids']==[11] and links[0]['relation']==''
    assert links[0]['matched_metric']=='fat'
    point['detail_evidence']=[]
    assert task.switch_health_links([point],'fat','increase')==[]


def test_all_changed_metrics_are_queried_in_normalized_magnitude_order():
    dims=[dim('fat',.1,.9,kind='higher_is_burden'),dim('fiber',1,4,5),dim('carbohydrate',1,3,5,kind='higher_is_burden'),dim('protein_quality',.5,.6)]
    with patch.object(task,'load_rules',return_value=[]),patch.object(task,'switch_health_rows',return_value=[]),patch.object(task,'load_market_advantages',return_value={}),patch.object(task,'switch_health_links',return_value=[]) as lookup:
        result=task.nutrition_pairs([pair(dims)],'cat')
    assert [call.args[1] for call in lookup.call_args_list]==['fat','fiber','carbohydrate','protein_quality']
    assert len(result['pairs'][0]['top_changes'])==4
