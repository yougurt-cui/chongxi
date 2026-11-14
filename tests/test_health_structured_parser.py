import copy
import json
import pytest
from vendor.chongxi_health_pipeline.structured_parser import validate_document,to_points
from vendor.chongxi_multi_model_mysql.run import extract_json


def doc():
    return {'question':'猫咪便便臭是什么原因','category':'invented_category','problem':'便臭',
      'knowledge_points':[{'cause':'蛋白质消化率下降','mechanism':'未吸收蛋白在后肠发酵产生臭味物质。',
       'manifestations':['软便'],'verification':{'supporting':['换粮后加重'],'opposing':[],'required_information':['饮食史']},
       'nutrition_factors':[{'factor':'protein_digestibility','direction':'decrease','relation':'消化率降低时未吸收底物增加。'}],
       'evidence_status':'conditional'}]}


def enums():
    return ({'DIGESTIVE':{'name':'消化与排便'}},{'STOOL_ODOR':{'name':'粪便异味加重','category_code':'DIGESTIVE'}},set())


def test_new_template_bypasses_dimensions_and_preserves_complete_group():
    d=doc();assert extract_json('模型前言\n```json\n'+json.dumps(d)+'\n```')==d
    rows=to_points(d,enums(),lambda *a:'STOOL_ODOR')
    assert len(rows)==1
    row=rows[0]
    for field in ('cause','mechanism','manifestations','verification','nutrition_factors','evidence_status'):
        assert row[field]==d['knowledge_points'][0][field]
    assert row['category_code']=='DIGESTIVE' and row['issue_code']=='STOOL_ODOR'
    assert row['source_items'][0]['header']['category']=='invented_category'


def test_subordinate_fields_stay_with_own_cause():
    d=doc();second=copy.deepcopy(d['knowledge_points'][0]);second['cause']='寄生虫感染';second['nutrition_factors']=[];second['manifestations']=['体重下降'];d['knowledge_points'].append(second)
    rows=to_points(d,enums(),lambda *a:'STOOL_ODOR')
    assert len(rows)==2 and rows[0]['nutrition_factors'] and not rows[1]['nutrition_factors']
    assert rows[0]['manifestations']==['软便'] and rows[1]['manifestations']==['体重下降']


@pytest.mark.parametrize('field,value',[('cause',''),('mechanism',None),('manifestations','软便'),('verification',{}),('nutrition_factors',{}),('evidence_status','invented')])
def test_invalid_group_rejected(field,value):
    d=doc();d['knowledge_points'][0][field]=value
    with pytest.raises(ValueError):validate_document(d)


def test_invalid_issue_cannot_enter_table():
    with pytest.raises(ValueError):to_points(doc(),enums(),lambda *a:'invented')


def test_storage_does_not_reference_removed_columns():
    from unittest.mock import MagicMock,patch
    from types import SimpleNamespace
    from services import health_knowledge_import_service as h
    from vendor.chongxi_multi_model_mysql import run
    rows=to_points(doc(),enums(),lambda *a:'STOOL_ODOR')
    cur=MagicMock()
    with patch.object(run,'provider_configuration',return_value=[SimpleNamespace(name='qwen',model='test')]):
        h.save_points(cur,{'id':1,'provider':'qwen','model':'test','search_metadata':'{}'},rows,enums(),'a'*64,'b'*64)
    sql=next(c.args[0] for c in cur.execute.call_args_list if 'INSERT INTO health_knowledge_point' in c.args[0])
    cols=sql.split('(',1)[1].split(')',1)[0].split(',')
    removed={'source_topic','knowledge_type','content','applicable_conditions','review_reasons','category_code','problem_code','problem_name','high_risk_warning'}
    assert not set(cols)&removed
    assert {'cause','mechanism','issue_code','nutrition_factors'}<=set(cols)
