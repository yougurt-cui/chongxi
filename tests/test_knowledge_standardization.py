import copy
import pytest
from services import knowledge_standardization_service as service


@pytest.fixture
def dictionary():
    return {'物种字典':[{'species_code':'cat'},{'species_code':'dog'}],
        '品种字典':[{'species_code':'cat','breed_code':'ragdoll'}],
        '生命阶段字典':[{'species_code':'cat','stage_code':s} for s in ['adult','unspecified','all_stages']],
        '用户需求字典':[{'need_code':'soft_stool'}],
        '知识大分类':[{'category_code':'feeding_nutrition'},{'category_code':'health_symptoms'}],
        '知识主题字典':[{'category_code':'feeding_nutrition','topic_code':'digestive_tolerance'}]}


def valid():
    return {'results':[{'id':1,'mappings':[{'original_condition':'布偶猫，易软便','species_code':'cat',
        'breed_code':'ragdoll','life_stage_codes':['all_stages'],'need_codes':['soft_stool'],
        'category_code':'feeding_nutrition','topic_code':'digestive_tolerance','notes':'阶段未指明，统一为全阶段'}]}]}


def test_legacy_unspecified_is_normalized(dictionary):
    response=valid()
    response['results'][0]['mappings'][0]['life_stage_codes']=['unspecified']
    result=service.validate_response(response,[{'id':1}],dictionary)
    assert result[1][0]['life_stage_codes']==['all_stages']


@pytest.mark.parametrize('field,value',[
    ('species_code','bird'),('breed_code','made_up'),('species_code','dog'),
    ('life_stage_codes',['adult','unspecified']),('life_stage_codes',['all_stages','adult']),
    ('life_stage_codes',['kitten']),('need_codes',['invented']),('need_codes',['soft_stool','soft_stool']),
    ('category_code','health_symptoms'),('topic_code','invented'),('life_stage_codes','adult'),
    ('notes',None),('breed_code',[]),
])
def test_rejects_invented_or_inconsistent_codes(dictionary,field,value):
    response=valid();response['results'][0]['mappings'][0][field]=value
    with pytest.raises(ValueError):service.validate_response(response,[{'id':1}],dictionary)


def test_unknown_fields_remain_null_and_empty(dictionary):
    response=valid();m=response['results'][0]['mappings'][0]
    for field in ['species_code','breed_code','category_code','topic_code']:m[field]=None
    m['need_codes']=[];m['life_stage_codes']=[]
    assert service.validate_response(response,[{'id':1}],dictionary)[1][0]['species_code'] is None


@pytest.mark.parametrize('change',['missing','duplicate','extra','empty'])
def test_rejects_missing_duplicate_or_extra_point_ids(dictionary,change):
    response=valid()
    if change=='missing':response['results']=[]
    if change=='duplicate':response['results']*=2
    if change=='extra':response['results'][0]['id']=2
    if change=='empty':response['results'][0]['mappings']=[]
    with pytest.raises(ValueError):service.validate_response(response,[{'id':1}],dictionary)


def test_multiple_scope_mappings_are_preserved(dictionary):
    response=valid();m=copy.deepcopy(response['results'][0]['mappings'][0]);m['life_stage_codes']=['adult']
    response['results'][0]['mappings'].append(m)
    assert len(service.validate_response(response,[{'id':1}],dictionary)[1])==2


def test_fingerprint_ignores_dict_key_order_but_tracks_content():
    assert service.fingerprint({'b':2,'a':1})==service.fingerprint({'a':1,'b':2})
    assert service.fingerprint({'content':'旧'})!=service.fingerprint({'content':'新'})


def test_model_receives_validation_feedback_for_missing_point(monkeypatch,dictionary):
    import json
    import sys
    import types
    invalid={'results':[]}
    responses=[invalid,valid()];calls=[]
    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(responses.pop(0))))])
    monkeypatch.setitem(sys.modules,'openai',types.SimpleNamespace(OpenAI=lambda **kwargs:types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))))
    provider=types.SimpleNamespace(key='test',base_url='https://example.com',model='test-model')
    rows=[{'id':1,'applicable_conditions':['布偶猫，易软便'],'topic':'肠胃营养','content':'正文'}]
    value,_=service.call_model(provider,rows,dictionary)
    assert value==valid() and len(calls)==2
    assert '遗漏知识点' in calls[1]['messages'][-1]['content']
    assert '不能把topic_code填进need_codes' in calls[1]['messages'][-1]['content']


def test_mapping_must_keep_complete_original_condition(dictionary):
    response=valid()
    rows=[{'id':1,'applicable_conditions':['布偶猫成猫','肠胃敏感']}]
    with pytest.raises(ValueError,match='完整条件'):
        service.validate_response(response,rows,dictionary)
    response['results'][0]['mappings'][0]['original_condition']='布偶猫成猫；肠胃敏感'
    assert service.validate_response(response,rows,dictionary)


def test_constraint_discards_unknown_need_and_copies_full_scope(dictionary):
    value=valid();m=value['results'][0]['mappings'][0]
    m['need_codes']=['soft_stool','digestive_tolerance'];m['original_condition']='省略的条件'
    rows=[{'id':1,'applicable_conditions':['布偶猫成猫','肠胃敏感']}]
    result=service.constrain_response(value,rows,dictionary)
    mapping=service.validate_response(result,rows,dictionary)[1][0]
    assert mapping['original_condition']=='布偶猫成猫；肠胃敏感'
    assert mapping['need_codes']==['soft_stool']
    assert 'digestive_tolerance' in mapping['notes'] and '未映射' in mapping['notes']


def test_topic_pairs_are_valid_unique_category_topic_pairs():
    mappings=valid()['results'][0]['mappings']
    assert service.topic_pairs(mappings+mappings)==[{'category_code':'feeding_nutrition','topic_code':'digestive_tolerance'}]
    assert service.topic_pairs([dict(mappings[0],category_code=None,topic_code=None)])==[]


def test_direct_mapping_uses_original_input_and_preserves_reimport_identity():
    from services import model_knowledge_import_service as store
    raw={'applicable_conditions':['布偶猫，易软便'],'topic':'原始肠胃主题','content':'正文保持原样'}
    direct={'applicable_conditions':valid()['results'][0]['mappings'],
        'topic':[{'category_code':'feeding_nutrition','topic_code':'digestive_tolerance'}],
        'content':raw['content'],'standardization_meta':{'raw_input':{k:raw[k] for k in ('applicable_conditions','topic')}}}
    assert service.source_value(direct)==raw
    assert store._triples([direct])==store._triples([raw])
    pending=service.pending_conditions(raw)
    assert set(pending[0])==service.KEYS
    assert pending[0]['original_condition']=='布偶猫，易软便'


def test_column_mapping_values_roundtrip_and_source_grouping():
    mapping=valid()['results'][0]['mappings'][0]
    values=service.mapping_values(mapping)
    columns=dict(zip(service.MAPPING_COLUMNS,values))
    assert service.stored_mapping(columns)=={k:mapping[k] for k in service.MAPPING_COLUMNS}
    raw={'applicable_conditions':['布偶猫，易软便'],'topic':'肠胃','content':'正文'}
    row={**columns,'id':1,'batch_id':1,'content':raw['content'],'topic':[],
         'standardization_meta':{'raw_input':{k:raw[k] for k in ('applicable_conditions','topic')}}}
    sibling={**row,'id':2,'breed_code':'unknown_cat'}
    assert service.unique_sources([row,sibling])==[row]
    assert service.source_value(row)==raw


def test_mapping_values_normalizes_legacy_stage():
    mapping=valid()['results'][0]['mappings'][0]
    mapping['life_stage_codes']=['unspecified']
    stored=dict(zip(service.MAPPING_COLUMNS,service.mapping_values(mapping)))
    assert service.stored_mapping(stored)['life_stage_codes']==['all_stages']
    assert mapping['life_stage_codes']==['unspecified']
