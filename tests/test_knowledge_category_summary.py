import sys,importlib.util,copy,json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from services import knowledge_source_assembly_service as s


def row(i=1,provider='qwen',content='观点A',stage='all_stages'):
    return {'id':i,'batch_id':i,'source_provider':provider,'source_model':provider+'-model',
        'species_code':'cat','breed_code':'ragdoll','life_stage_codes':[stage],
        'category_code':'feeding_nutrition','topic_code':'food_selection',
        'content':content,'need_codes':[],'standardization_meta':{'status':'success'}}


def test_category_is_a_row_and_multiple_models_share_one_group():
    groups,u,p=s.category_groups([row(),row(2,'deepseek','观点B')])
    assert list(groups)==[('cat','ragdoll','all_stages','unspecified','food_selection')]
    assert len(next(iter(groups.values())))==2
    assert not u and not p


def test_equal_text_retains_both_model_sources():
    groups,_,_=s.category_groups([row(),row(2,'deepseek')]);entries=next(iter(groups.values()))
    assert len(entries)==1 and len(entries[0]['sources'])==2


def test_objects_stages_and_categories_do_not_cross_merge():
    rows=[row(),row(2,stage='adult'),dict(row(3),breed_code='all_cat'),dict(row(4),topic_code='digestive_tolerance')]
    groups,_,_=s.category_groups(rows)
    assert len(groups)==4


def entries():return next(iter(s.category_groups([row(),row(2,'deepseek','观点B')])[0].values()))


def test_summary_refs_are_complete_and_verifiable():
    value={'points':[{'content':'合并观点','source_ids':[1,2]}],'notes':['待核验']}
    assert s.validate_summary(value,entries())==value

@pytest.mark.parametrize('value',[
    {'points':[{'content':'合并','source_ids':[1]}],'notes':[]},
    {'points':[{'content':'合并','source_ids':[1,2,999]}],'notes':[]},
    {'points':[{'content':'','source_ids':[1,2]}],'notes':[]},
    {'points':[],'notes':[]},
    {'points':[{'content':'合并','source_ids':[1,2]}],'notes':'错误'},
])
def test_invalid_or_incomplete_summary_rejected(value):
    with pytest.raises(ValueError):s.validate_summary(value,entries())


def test_unchanged_inputs_reuse_summary_without_model_call(monkeypatch):
    rows=[row(),row(2,'deepseek','观点B')];groups,_,_=s.category_groups(rows);key=next(iter(groups))
    old=dict(zip(s.KEY_COLUMNS,key));old['summary_meta']={'status':'success','input_hash':s.digest([s.SUMMARY_VERSION,key,groups[key]])}
    connection=MagicMock();connection.__enter__.return_value=connection
    cursor=connection.cursor.return_value;cursor.__enter__.return_value=cursor
    cursor.fetchone.return_value={'acquired':1};cursor.fetchall.return_value=[old]
    monkeypatch.setattr(s.store,'connect',lambda:connection);monkeypatch.setattr(s,'fetch_rows',lambda:rows)
    def forbidden(*args):raise AssertionError('缓存命中不应调用模型')
    result=s.refresh(summarizer=forbidden,sync_scenes=False)
    assert result['reused']==1 and result['summarized']==0 and result['failed']==[]


def test_invalid_output_table_rejected():
    with pytest.raises(ValueError):s.refresh(table='arbitrary_table')


def test_summary_records_usage_and_repairs_refs_once_without_hidden_sdk_retries(monkeypatch,tmp_path):
    from types import SimpleNamespace
    import sys
    from vendor.chongxi_multi_model_mysql import run
    from services import knowledge_standardization_service as normalizer
    outputs=[{'points':[{'content':'合并观点','source_ids':[1]}],'notes':[]},
             {'points':[{'content':'合并观点','source_ids':[1,2]}],'notes':[]}]
    calls=[];initial=[]
    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(outputs.pop(0))))],
            usage=SimpleNamespace(prompt_tokens=12,completion_tokens=34))
    def client(**kwargs):
        initial.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules,'openai',SimpleNamespace(OpenAI=client))
    monkeypatch.setattr(run,'provider_configuration',lambda:[SimpleNamespace(name='qwen',key='test',model='test-model',base_url='https://example.com')])
    monkeypatch.setattr(normalizer,'standardization_paused',lambda:False)
    monkeypatch.setattr(s.store,'__file__',str(tmp_path/'services/store.py'))
    result=s.summarize(('cat','ragdoll','all_stages','unspecified','food_selection'),entries())
    assert len(calls)==2 and initial[0]['max_retries']==0
    assert calls[0]['max_tokens']==3000
    assert result['summary_meta']['usage']=={'calls':2,'prompt_tokens':24,'completion_tokens':68}
    assert '未经外部核验' in result['content']
    audit=(tmp_path/'var/multi-model/category-summary-usage.jsonl').read_text().splitlines()
    assert len(audit)==2


def test_same_object_and_category_with_different_need_scenes_do_not_merge():
    rows=[dict(row(),need_scene_code='food_transition'),dict(row(2,'deepseek'),need_scene_code='soft_stool')]
    groups,_,_=s.category_groups(rows)
    assert len(groups)==2
    assert {k[3] for k in groups}=={'food_transition','soft_stool'}
