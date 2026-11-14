import pytest
from vendor.chongxi_multi_model_mysql import run

@pytest.mark.parametrize('parsed,count',[(None,0),({'knowledge_points':[{},{}]},2),
    ({'dimensions':{'a':[{},{}],'b':[{}]}},3),({'dimensions':{}},0)])
def test_count_supports_health_and_dimension_documents(parsed,count):
    assert run.count_points(parsed)==count


def test_structured_health_result_does_not_interrupt_other_providers(monkeypatch):
    saved=[];statuses=[]
    def call(provider,prompt,**kwargs):
        return {'provider':provider.name,'status':'success','parsed':{'knowledge_points':[{},{}]},
                'latency_ms':1,'error':None}
    monkeypatch.setattr(run,'call_provider',call)
    monkeypatch.setattr(run,'persist_result',lambda task_id,result:saved.append(result['provider']))
    monkeypatch.setattr(run,'update_task',lambda task_id,status:statuses.append(status))
    providers=[run.Provider(n,'test','https://example.com','test') for n in ['qwen','doubao','deepseek']]
    assert run.execute_task(1,'测试',providers)=='completed'
    assert set(saved)=={'qwen','doubao','deepseek'} and len(saved)==3
    assert statuses==['completed']


def test_health_condition_string_keeps_whole_scope_as_single_array_item():
    import json
    document={'question':'猫咪便便臭是什么原因','category':'消化与排便','problem':'粪便异味',
      'knowledge_points':[{'cause_type':'diet','cause':'原因','mechanism':'可能的机制',
        'manifestations':[],'verification':{'supporting':[],'opposing':[],'required_information':[]},
        'nutrition_factors':[],'evidence_status':'conditional',
        'applicable_conditions':'换粮后或存在饮食调整；需结合饮食史'}]}
    raw=json.dumps(document,ensure_ascii=False)
    parsed=run.extract_json(raw)
    assert parsed['knowledge_points'][0]['applicable_conditions']==['换粮后或存在饮食调整；需结合饮食史']
    assert json.loads(raw)['knowledge_points'][0]['applicable_conditions']=='换粮后或存在饮食调整；需结合饮食史'
