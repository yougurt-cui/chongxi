import sys,importlib.util,copy
import pytest
from unittest.mock import MagicMock
from services import health_knowledge_summary_service as s


def row(i=1,provider='qwen'):
    return {'id':i,'species_code':'cat','breed_code':'unknown_cat','life_stage_code':'all_stages',
      'issue_code':'STOOL_ODOR','cause_type':'diet','cause':'消化利用变化','mechanism':'原始候选机制',
      'manifestations':['原始表现'+str(i)],'verification':{'supporting':['支持'+str(i)],'opposing':[],'required_information':['补充'+str(i)]},
      'nutrition_factors':[{'factor':'factor_'+str(i),'direction':'change','relation':'原始关联'}],
      'source_provider':provider,'source_model':'test','evidence_status':'conditional'}


def value(ids=(1,2)):
    refs=[{'source_id':i,'index':0} for i in ids]
    return {'chains':[{'cause':'合并原因','mechanism':'候选机制总结','source_ids':list(ids),
      'manifestation_refs':copy.deepcopy(refs),'nutrition_refs':copy.deepcopy(refs),
      'verification_refs':{'supporting':copy.deepcopy(refs),'opposing':[],'required_information':copy.deepcopy(refs)}}],
      'notes':['候选知识']}


def test_same_scope_and_cause_type_group_models_together():
    groups,pending=s.group_rows([row(),row(2,'deepseek')])
    assert len(groups)==1 and len(next(iter(groups.values())))==2 and not pending


def test_species_breed_stage_issue_and_cause_type_do_not_cross_merge():
    rows=[row(),dict(row(2),cause_type='disease'),dict(row(3),life_stage_code='adult'),dict(row(4),issue_code='SOFT_STOOL'),dict(row(5),breed_code='ragdoll')]
    assert len(s.group_rows(rows)[0])==5


def test_materialized_fields_are_verbatim_and_remain_with_their_chain():
    rows=[row(),row(2,'deepseek')];result=s.materialize(value(),rows)
    chain=result['causal_chains'][0]
    assert result['manifestations']==['原始表现1','原始表现2']
    assert chain['manifestations']==['原始表现1','原始表现2']
    assert chain['verification']['supporting']==['支持1','支持2']
    assert chain['nutrition_factors']==[rows[0]['nutrition_factors'][0],rows[1]['nutrition_factors'][0]]
    assert result['nutrition_factors'][0]['cause']=='合并原因'


def test_different_causes_in_same_type_remain_separate():
    rows=[row(),row(2,'deepseek')]
    v=value((1,));second=value((2,))['chains'][0];second['cause']='另一个独立原因';v['chains'].append(second)
    result=s.materialize(v,rows)
    assert len(result['causal_chains'])==2
    assert result['causal_chains'][0]['verification']['supporting']==['支持1']
    assert result['causal_chains'][1]['verification']['supporting']==['支持2']

@pytest.mark.parametrize('change',['missing_source','missing_detail','invented_source','index_out_of_range','cross_chain'])
def test_invalid_or_incomplete_refs_rejected(change):
    v=value();rows=[row(),row(2,'deepseek')]
    if change=='missing_source':v['chains'][0]['source_ids']=[1]
    if change=='missing_detail':v['chains'][0]['verification_refs']['supporting']=[]
    if change=='invented_source':v['chains'][0]['source_ids'].append(99)
    if change=='index_out_of_range':v['chains'][0]['nutrition_refs'][0]['index']=99
    if change=='cross_chain':
        v=value((1,));v['chains'].append(value((2,))['chains'][0]);v['chains'][0]['nutrition_refs']=[{'source_id':2,'index':0}]
    with pytest.raises(ValueError):s.validate_summary(v,rows)


def test_unknown_scope_is_pending_not_combined():
    groups,pending=s.group_rows([dict(row(),breed_code=None)])
    assert not groups and pending==[1]


def test_unchanged_group_uses_cache(monkeypatch):
    rows=[row(),row(2,'deepseek')];groups,_=s.group_rows(rows);key=next(iter(groups))
    old=dict(zip(s.KEYS,key));old['summary_meta']={'status':'success','input_hash':s.digest([s.VERSION,key,groups[key]])}
    connection=MagicMock();connection.__enter__.return_value=connection
    cursor=connection.cursor.return_value;cursor.__enter__.return_value=cursor
    cursor.fetchone.return_value={'acquired':1};cursor.fetchall.return_value=[old]
    monkeypatch.setattr(s.store,'connect',lambda:connection);monkeypatch.setattr(s,'fetch_rows',lambda *args:rows)
    def forbidden(*args):raise AssertionError('缓存命中禁止模型调用')
    result=s.refresh(summarizer=forbidden)
    assert result['reused']==1 and result['summarized']==0 and not result['failed']


def test_singleton_index_repair_is_unambiguous_and_logged():
    rows=[row(),row(2,'deepseek')];v=value()
    v['chains'][0]['verification_refs']['supporting'][1]['index']=1
    repaired=s.constrain_singleton_refs(v,rows)
    s.validate_summary(repaired,rows)
    assert repaired['chains'][0]['verification_refs']['supporting'][1]['index']==0
    assert '下标归一' in repaired['notes'][-1]


def test_multiple_possible_items_are_never_guessed():
    rows=[row(),row(2,'deepseek')];rows[1]['verification']['supporting'].append('另一个依据')
    v=value();v['chains'][0]['verification_refs']['supporting'][1]['index']=9
    s.constrain_singleton_refs(v,rows)
    assert v['chains'][0]['verification_refs']['supporting'][1]['index']==9
    with pytest.raises(ValueError):s.validate_summary(v,rows)
