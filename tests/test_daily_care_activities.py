import copy
import json
from pathlib import Path
from unittest.mock import patch,MagicMock
import pytest
from vendor.chongxi_care_import import import_care as parser
from services import daily_care_knowledge_service as care
from services.daily_care_activity_adapter import points_for,dictionary_scope
from vendor.chongxi_multi_model_mysql import run

SAMPLE=Path(__file__).resolve().parents[1]/'vendor/chongxi_care_import/sample_input.json'


def document():
    return json.loads(SAMPLE.read_text())


def v4_document():
    doc=document();a=doc['care_activities'][0]
    doc.update(species='unknown',breed='unknown',life_stage='unknown')
    a.update(species='dog',breed='all',life_stage='adult_dog',care_subtype='internal',need_code='parasite_prevention',
             applicable_conditions={'age_range':None,'weight_range':None,'administration_route':'oral','health_conditions':[]},
             schedule={'frequency':0,'frequency_unit':'conditional','frequency_basis':'产品标签与风险评估'})
    return doc


def test_v3_and_v4_scopes_and_six_fields_are_lossless():
    old=parser.parse_document(document(),'test')[0]
    assert old['species']=='cat' and old['life_stage']=='unknown' and old['care_subtype']=='unknown'
    source=v4_document();row=parser.parse_document(source,'test')[0]
    assert [row[k] for k in ('species','breed','life_stage','care_subtype')]==['dog','all','adult_dog','internal']
    assert row['original_payload']==source['care_activities'][0]
    for key in ('schedule','tools_materials','methods','avoidances','observations','adjustment_rules','applicable_conditions'):
        assert row[key]==source['care_activities'][0][key]
    assert row['schedule']['frequency']==0
    point=points_for(source)[0]
    assert point['care_activity']==source['care_activities'][0]
    assert dictionary_scope(point)=={'species':'dog','breed':'all_dog','life_stage':'adult'}
    assert source==v4_document()


def test_custom_subtype_and_need_not_invented():
    doc=v4_document();doc['care_activities'][0].update(care_subtype='routine_brushing',care_category='常规体检',need_code='')
    row=parser.parse_document(doc)[0]
    assert row['care_subtype']=='routine_brushing' and row['need_code']=='unknown'


def test_dedup_does_not_merge_distinct_conditions_or_rewrite_scenarios():
    doc=v4_document();a=copy.deepcopy(doc['care_activities'][0]);a['applicable_conditions']['age_range']='成年犬，具体适用需审核'
    doc['care_activities'] += [a,copy.deepcopy(a)]
    rows=parser.parse_document(doc)
    assert len(rows)==2 and len({r['knowledge_key'] for r in rows})==2
    assert len(points_for(doc))==2


@pytest.mark.parametrize('field,value',[('schedule',[]),('observations',[]),('methods',{}),('care_activity_code',''),('life_stage',123)])
def test_invalid_types_rejected(field,value):
    doc=v4_document();doc['care_activities'][0][field]=value
    with pytest.raises(ValueError):care.validate_document(doc)


def test_extract_json_and_prompt_substitution_do_not_replace_user_text():
    doc=v4_document();raw='说明\n```json\n'+json.dumps(doc,ensure_ascii=False)+'\n```'
    parsed=run.extract_json(raw,knowledge_base='daily_care')
    assert parsed['care_activities']==doc['care_activities'] and parsed['knowledge_base']=='daily_care'
    assert run.count_points(parsed)==1
    result=care.render_prompt('{{question}} / {{species}} / {{breed}} / {{life_stage}} / {{existing_care_activities}} / {{existing_observations}} / {{reference_materials}}','猫 {{species}}')
    assert result.startswith('猫 {{species}} / unknown / unknown / unknown / [] / [] / ')


def test_literal_subset_rejects_numbers_and_wrong_fields():
    assert parser.literal_subset({'frequency':0},{'frequency':0,'unit':'day'})
    assert not parser.literal_subset({'frequency':20},{'frequency':0})
    assert not parser.literal_subset({'dose':0},{'frequency':0})
    assert not parser.literal_subset(True,1)


def test_activity_import_keeps_scope_and_review_with_existing_writer():
    from services import model_knowledge_import_service as store
    conn=MagicMock();conn.__enter__.return_value=conn;cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value={'id':5,'knowledge_base':'daily_care','status':'success','question':'驱虫','raw_response':json.dumps(v4_document())}
    cur.fetchall.return_value=[]
    dictionary={'species':[{'species_code':'dog'}],'breeds':[{'species_code':'dog','breed_code':'all_dog','breed_name':'全部犬种'}],'stages':[{'species_code':'dog','stage_code':'adult'}]}
    with patch('services.daily_care_fusion_service.acquire_lock',return_value='qa-lock'),patch('services.daily_care_fusion_service.rebuild',return_value={'total':1}),patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn),patch.object(care,'load_dictionary',return_value=dictionary),patch.object(run,'call_provider') as model:
        assert care.import_batch(5)['inserted_count']==1
        sql,params=next(c.args for c in cur.execute.call_args_list if c.args[0].startswith('INSERT'))
        assert 'daily_care_knowledge_point_detail' in sql and 'all_dog' in params and 'adult' in params
        model.assert_not_called()


def test_read_input_accepts_utf8_bom_and_fences(tmp_path):
    path=tmp_path/'response.json';path.write_text('\ufeff```json\n'+json.dumps(v4_document())+'\n```',encoding='utf-8')
    assert parser.read_input(path)==v4_document()


@pytest.mark.parametrize('change,expected',[('same',None),('number','added/rewrote'),('drop','dropped'),('scope','explicit source scope')])
def test_optional_partition_literal_validation(change,expected):
    from io import BytesIO
    doc=v4_document();activity=doc['care_activities'][0]
    part={k:copy.deepcopy(activity.get(k)) for k in ('life_stage','care_subtype','applicable_conditions','schedule','tools_materials','methods','avoidances','observations','adjustment_rules')}
    if change=='number':part['schedule']['frequency']=10
    if change=='drop':part['methods']=[]
    if change=='scope':part['life_stage']='kitten'
    response=BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'rows':[part]})}}]}).encode())
    with patch.object(parser,'urlopen',return_value=response):
        if expected:
            with pytest.raises(ValueError,match=expected):parser.llm_partition(doc,activity,'test','mock','https://example.com/v1','test')
        else:
            rows=parser.llm_partition(doc,activity,'test','mock','https://example.com/v1','test')
            assert rows[0]['schedule']['frequency']==0
