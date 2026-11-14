import copy
import pytest
from vendor.chongxi_health_pipeline.task_parser import sources_from_dimensions, classify_sources
from vendor.chongxi_health_pipeline import health_pipeline


def doc():
    return {'question':'猫咪软便如何观察？','dimensions':{'applicable_object':[],
        'selection_points':[{'content':'记录换粮时间。','topic':'软便','applicable_conditions':['猫'], 'evidence_status':'unknown'}],
        'product_recommendations':[],'product_content':[],
        'precautions':[{'content':'观察粪便形态变化。','topic':'软便','applicable_conditions':[],
                        'evidence_status':'uncertain','source_urls':['https://example.com/source']}]}}


def enums():
    return ({'DIGESTIVE':{'name':'消化与排便'}},
            {'SOFT_STOOL':{'name':'软便','aliases':[],'category_code':'DIGESTIVE'}},
            {'CAUSE','MECHANISM','MANIFESTATION','CONDITION','DEFINITION'})


def test_five_dimensions_preserve_every_source_and_evidence():
    sources=sources_from_dimensions(doc())
    points=classify_sources(sources,doc()['question'],enums())
    assert len(points)==2
    for source,point in zip(sources,points):
        for key,value in source.items():assert point[key]==value
        assert point['problem_code']=='SOFT_STOOL'
        assert point['knowledge_type'] is None  # care advice never inferred as a cause
        assert point['review_status']=='pending'
    assert points[1]['source_urls']==['https://example.com/source']


def test_classifier_only_changes_codes_never_content():
    sources=sources_from_dimensions(doc())
    def classifier(s,q,e):
        return {'results':[{'source_id':x['source_id'],'problem_code':'SOFT_STOOL','knowledge_type':'CONDITION'} for x in s]}
    result=classify_sources(sources,'question',enums(),classifier)
    assert [p['content'] for p in result]==[s['content'] for s in sources]
    assert all(p['review_required'] for p in result)


@pytest.mark.parametrize('mode',['missing','duplicate','invented_code','invented_type','rewritten_content'])
def test_invalid_classification_is_rejected(mode):
    sources=sources_from_dimensions(doc())
    value={'results':[{'source_id':x['source_id'],'problem_code':'SOFT_STOOL','knowledge_type':None} for x in sources]}
    if mode=='missing':value['results'].pop()
    if mode=='duplicate':value['results'][1]=copy.deepcopy(value['results'][0])
    if mode=='invented_code':value['results'][0]['problem_code']='FAKE'
    if mode=='invented_type':value['results'][0]['knowledge_type']='ADVICE'
    if mode=='rewritten_content':value['results'][0]['content']='invented'
    with pytest.raises(ValueError):classify_sources(sources,'',enums(),lambda *args:value)


def test_unknown_problem_does_not_use_substring_match():
    source=sources_from_dimensions(doc())[0];source['source_topic']='软便或其他未明症状'
    p=classify_sources([source],'',enums())[0]
    assert p['problem_code'] is None and p['review_reasons']


def test_old_package_now_accepts_actual_page_format():
    result=health_pipeline.transform(doc(),enums(),{})
    assert result['meta']['point_count']==2
    assert result['knowledge_points'][0]['evidence_status']=='unknown'


def test_empty_or_malformed_dimensions_do_not_silently_succeed():
    d=doc()
    for dimension in d['dimensions']:d['dimensions'][dimension]=[]
    with pytest.raises(ValueError):sources_from_dimensions(d)
    d=doc();d['dimensions']['precautions'][0]['applicable_conditions']='cat'
    with pytest.raises(ValueError):sources_from_dimensions(d)


def test_unchanged_batch_skips_model_and_preserves_records(monkeypatch):
    from unittest.mock import MagicMock
    from services import health_knowledge_import_service as h
    from services import model_knowledge_import_service as s
    from vendor.chongxi_health_pipeline.task_parser import digest
    from services.health_knowledge_import_service import VERSION
    batch={'id':123,'question':doc()['question'],'parsed_response':s.dumps(doc()),
           'knowledge_base':'health','status':'success','search_metadata':'{}'}
    state={'input_hash':digest({'question':batch['question'],'response':doc(),'search_metadata':{}}),
           'dictionary_hash':digest([enums()[0],enums()[1],sorted(enums()[2])]),
           'mapping_version':VERSION,'point_count':2}
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=MagicMock();conn.cursor.return_value.__enter__.return_value=cur
    cur.fetchone.side_effect=[{'acquired':1},batch,state]
    monkeypatch.setattr(h,'ensure_tables',lambda:None)
    monkeypatch.setattr(h,'load_enums',enums)
    monkeypatch.setattr(h.store,'connect',lambda:conn)
    classifier=MagicMock(side_effect=AssertionError('Must not call model again'))
    result=h.import_batch(123,classifier=classifier)
    assert result['health_status']=='already_imported'
    classifier.assert_not_called()
    assert not any('DELETE' in call.args[0] for call in cur.execute.call_args_list)


@pytest.mark.parametrize('evidence',['supported','conditional','limited','insufficient'])
def test_six_dimension_health_task_is_valid_at_collection_and_import(evidence):
    from vendor.chongxi_health_pipeline.task_parser import SIX_DIMENSIONS
    from vendor.chongxi_multi_model_mysql.run import extract_json
    import json
    d={'question':'猫咪便便臭是什么原因','dimensions':{k:[] for k in SIX_DIMENSIONS}}
    d['dimensions']['health_risks']=[{'content':'原始知识正文','topic':'粪便异味加重',
        'applicable_conditions':['猫'],'evidence_status':evidence,'nutrition_factors':[]}]
    parsed=extract_json('模型前言\n```json\n'+json.dumps(d)+'\n```')
    sources=sources_from_dimensions(parsed)
    assert len(sources)==1 and sources[0]['source_dimension']=='health_risks'
    assert sources[0]['evidence_status']==evidence


def test_mixed_dimension_schemas_are_rejected():
    from vendor.chongxi_multi_model_mysql.run import extract_json
    import json
    d=doc();d['dimensions']['health_risks']=[]
    with pytest.raises(ValueError):extract_json(json.dumps(d))


def test_nutrition_factors_preserve_every_field():
    d=doc()
    factor={'factor':'crude_protein','direction':'increase','explanation':'原始关联说明','condition':'仅满足原文条件时'}
    d['dimensions']['precautions'][0]['nutrition_factors']=[factor]
    points=classify_sources(sources_from_dimensions(d),'',enums())
    assert points[1]['nutrition_factors']==[factor]
    assert points[0]['nutrition_factors']==[]
    assert points[1]['review_status']=='pending'


@pytest.mark.parametrize('factors',[None,{},[{'factor':'protein'}],[{'factor':'protein','direction':'wrong','explanation':'x','condition':'x'}]])
def test_malformed_nutrition_factors_are_not_silently_dropped(factors):
    d=doc();d['dimensions']['precautions'][0]['nutrition_factors']=factors
    with pytest.raises(ValueError):sources_from_dimensions(d)


def test_save_points_has_no_card_table_dependency():
    from unittest.mock import MagicMock
    from services import health_knowledge_import_service as h
    from vendor.chongxi_multi_model_mysql import run
    from types import SimpleNamespace
    from unittest.mock import patch
    cur=MagicMock();points=classify_sources(sources_from_dimensions(doc()),'',enums())
    with patch.object(run,'provider_configuration',return_value=[SimpleNamespace(name='qwen',model='test-model')]):
        h.save_points(cur,{'id':1,'provider':'qwen','model':'test-model','search_metadata':'{}'},points,enums(),'a'*64,'b'*64)
    queries=[c.args[0] for c in cur.execute.call_args_list]
    assert all('health_knowledge_card' not in q and 'card_id' not in q for q in queries)
    assert any('nutrition_factors' in q for q in queries)
