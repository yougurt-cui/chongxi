import copy
import json
from unittest.mock import MagicMock,patch
import pytest
from services import solution_summary_service as summary,solution_scene_service as scenes
from tests.test_solution_scene import source


def sources():return next(iter(scenes.source_groups([source(),source(2,'deepseek',solution=['另一套步骤'])]).values()))


def value_for(sources):
    active={i:s for i,s in enumerate(sources) if s['review_status']!='rejected'}
    fields={field:[{'text':text,'refs':[{'source_id':i,'index':j}]} for i,s in active.items() for j,text in enumerate(summary.source_values(s,field))] for field in summary.FIELDS}
    return {**fields,'plans':[{'source_ids':[i],
        'steps':[{'action':text,'refs':[{'source_id':i,'index':j}]} for j,text in enumerate(s['scenario_data']['solution'])],
        'verification':{k:{'text':v,'source_ids':[i]} for k,v in s['scenario_data']['verification'].items()}}
        for i,s in active.items()],'notes':['待审核'],'disagreements':[]}


def fake_summary(key,sources):
    return value_for(sources),{'status':'success','version':summary.VERSION,'input_hash':summary.fingerprint(key,sources),'model':'test-model','notes':[]}


def test_summary_keeps_alternative_plans_and_supporting_references():
    s=sources();v=value_for(s);assert summary.validate(v,s)==v
    row=summary.materialize(v,s)
    assert [step['plan_id'] for step in row['steps']]==[1,2]
    assert '【方案 1】' in row['verification']['next_action']
    assert row['steps'][1]['source_refs']==[{'source_id':1,'index':0}]


@pytest.mark.parametrize('change', ['drop_plan','drop_step','bad_reference','mix_plans','novel_number','cross_verification','rejected_reference'])
def test_invalid_summary_never_materializes(change):
    s=sources();v=value_for(s)
    if change=='drop_plan':v['plans'].pop()
    if change=='drop_step':v['plans'][0]['steps']=[]
    if change=='bad_reference':v['possible_causes'][0]['refs'][0]['index']=123
    if change=='mix_plans':v['plans'][0]['source_ids']=[0,1];v['plans'].pop()
    if change=='novel_number':v['plans'][0]['steps'][0]['action']='服用 999 mg'
    if change=='cross_verification':v['plans'][0]['verification']['next_action']['source_ids']=[1]
    if change=='rejected_reference':s[0]['review_status']='rejected'
    with pytest.raises(ValueError):summary.materialize(v,s)


def test_refresh_updates_pending_then_reuses_summary_and_protects_review():
    s=sources();key=scenes.identity(s[0]['scenario_data']);base=scenes.materialize(key,s)
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    old={**base,'id':7,'review_status':'pending'};cur.fetchall.return_value=[old]
    with patch('services.solution_knowledge_service.source_rows',return_value=[source(),source(2,'deepseek',solution=['另一套步骤'])]):
        result=scenes.refresh(conn,summarizer=fake_summary);assert result['changed_count']==1
        statement=next(c.args for c in cur.execute.call_args_list if c.args[0].startswith('UPDATE'))
        assert 'summary_meta' in statement[0]
        old.update(summary_meta=fake_summary(key,s)[1],input_hash=summary.fingerprint(key,s),review_status='verified')
        with patch.object(summary,'summarize',side_effect=AssertionError('no extra call')):
            assert scenes.refresh(conn)['changed_count']==0
        old['summary_meta']=None
        with patch.object(summary,'summarize') as model:
            with pytest.raises(ValueError,match='人工审核'):scenes.refresh(conn)
            model.assert_not_called()
