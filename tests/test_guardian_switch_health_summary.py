from unittest.mock import patch
from services import guardian_task_knowledge as task
from test_guardian_task_knowledge import connection


def chain(cause,code,direction,relation,manifestations):
    return dict(cause=cause,nutrition_factors=[dict(factor=code,direction=direction,relation=relation)],manifestations=manifestations,source_ids=[11])


def test_exact_chain_only_and_chinese_alias_and_remedy_exclusion():
    points=[dict(id=1,issue_code='CONSTIPATION',manifestations=['不能读取整行'],causal_chains=[
        chain('膳食纤维摄入不足','纤维支持','decrease','纤维支持不足时，排便支持可能减弱。',['粪便干硬']),
        chain('饮水不足','OTHER','decrease','其他原因',['饮水少']),
        chain('纤维摄入不足','FIBER_SUPPORT','increase','适量增加纤维可改善排便。',['排便费力'])])]
    links=task.switch_health_links(points,'fiber','decrease')
    assert len(links)==1 and links[0]['manifestations']==['粪便干硬']
    assert links[0]['source_table']=='health_knowledge_point' and links[0]['chain_index']==0
    assert task.switch_health_links(points,'fiber','increase')==[]
    assert task.switch_health_links([dict(id=2,nutrition_factors=[],manifestations=['整行表现'])],'fiber','decrease')==[]


def test_different_chains_same_summary_are_not_merged():
    points=[dict(id=1,issue_code='SOFT_STOOL',causal_chains=[chain('脂肪摄入过高','FAT_BURDEN','increase','条件甲',['表现甲']),chain('高脂饮食','FAT_BURDEN','increase','条件乙',['表现乙'])])]
    links=task.deduplicate_health_links(task.switch_health_links(points,'fat','increase'))
    assert len(links)==2 and links[0]['cause']=='脂肪摄入过高' and links[1]['cause']=='高脂饮食'


def test_summary_reader_species_parameter_and_parses_chains():
    c,q=connection([dict(id=1,causal_chains='[]')])
    with patch.object(task,'_connect',return_value=c):
        assert task.switch_health_rows(None)==[]
        q.execute.assert_not_called()
        assert task.switch_health_rows('cat')[0]['causal_chains']==[]
    sql,params=q.execute.call_args.args
    assert 'health_knowledge_point_detail' not in sql and 'health_knowledge_point' in sql
    assert params==['cat']
