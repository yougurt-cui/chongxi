import pytest
from services import behavior_knowledge_summary_service as s


def source(i):return {'id':i,'data':{'time':'3天','conditions':['进食后']}}
def value():return {'normal_scenarios':[{'text':'进食后，原文观察3天','source_detail_ids':[1,2]}],'abnormal_scenarios':[],'unmapped_health_issues':[],'notes':[],'disagreements':[]}


def test_summary_requires_all_sources_and_no_invented_values():
    sources=[source(1),source(2)]
    assert s.validate(value(),sources)==value()
    v=value();v['normal_scenarios'][0]['source_detail_ids']=[1]
    with pytest.raises(ValueError,match='遗漏'):s.validate(v,sources)
    v=value();v['normal_scenarios'][0]['text']='观察7天'
    with pytest.raises(ValueError,match='数值'):s.validate(v,sources)
    v=value();v['normal_scenarios'][0]['source_detail_ids']=[3]
    with pytest.raises(ValueError,match='引用'):s.validate(v,sources)


def test_same_behavior_name_merges_code_variants_but_preserves_life_stage():
    class Cursor:
        def execute(self,*args):pass
        def fetchall(self):
            return [{'id':i,'batch_id':i,'species_code':'cat','breed_code':'unknown_cat','life_stage_code':stage,
                     'category_code':'other','observation_code':code,'behavior_name':'吐毛球','review_status':'pending',
                     'source_provider':provider,'source_model':provider,'source_urls':[], 'sources':[], 'summary_meta':{},
                     'point_data':{'observation':{'observation_name':'呕吐毛球'}}}
                    for i,stage,code,provider in [(1,'adult','vomiting_hairball','qwen'),(2,'unspecified','VOMIT_HAIRBALL','doubao'),(3,'unspecified','obs_cat_hairball','deepseek')]]
    groups=s.groups(Cursor())
    assert sorted(len(g['sources']) for g in groups.values())==[1,2]
    assert len({src['id'] for g in groups.values() for src in g['sources']})==3
