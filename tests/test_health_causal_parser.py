import copy
import pytest
from vendor.chongxi_health_pipeline.causal_parser import parse_chains


def setup():
    sources=[{'source_id':'health_risks:0','source_dimension':'health_risks','source_index':0,
        'content':'蛋白质消化不足使未消化蛋白进入结肠发酵，产生臭味。可伴随软便。','source_topic':'便臭',
        'applicable_conditions':['换粮后出现'],'nutrition_factors':[{'factor':'protein','direction':'change','explanation':'原文说明','condition':'原文条件'}],
        'evidence_status':'conditional','source_urls':[]}]
    enums=({'DIGESTIVE':{'name':'消化与排便'}},{'STOOL_ODOR':{'category_code':'DIGESTIVE','name':'粪便异味加重'}},set())
    q=lambda text:{'source_id':'health_risks:0','text':text}
    value={'chains':[{'issue_code':'STOOL_ODOR','cause':q('蛋白质消化不足'),'mechanism':q('未消化蛋白进入结肠发酵，产生臭味'),
        'manifestations':[q('软便')],'verification':{'supporting':[q('换粮后出现')],'opposing':[],'required_information':[]},
        'nutrition_refs':[{'source_id':'health_risks:0','factor_index':0}]}],'excluded':[]}
    return sources,enums,value


def test_one_row_contains_cause_mechanism_and_dependents():
    sources,enums,value=setup();points,excluded=parse_chains(sources,'便臭',enums,lambda *a:value)
    assert len(points)==1 and not excluded
    p=points[0]
    assert p['cause']=='蛋白质消化不足' and p['manifestations']==['软便']
    assert p['verification']['supporting']==['换粮后出现']
    assert p['nutrition_factors']==sources[0]['nutrition_factors']
    assert p['category_code']=='DIGESTIVE' and p['issue_code']=='STOOL_ODOR'


@pytest.mark.parametrize('mode',['invented','missing_mechanism','bad_issue','bad_factor','uncovered','cross_cause'])
def test_invalid_chains_rejected(mode):
    sources,enums,value=setup()
    if mode=='invented':value['chains'][0]['mechanism']['text']='不存在的机制'
    if mode=='missing_mechanism':value['chains'][0]['mechanism']['text']=''
    if mode=='bad_issue':value['chains'][0]['issue_code']='MADE_UP'
    if mode=='bad_factor':value['chains'][0]['nutrition_refs'][0]['factor_index']=4
    if mode in ('uncovered','cross_cause'):
        extra=copy.deepcopy(sources[0]);extra['source_id']='health_risks:1';sources.append(extra)
        if mode=='cross_cause':value['chains'][0]['mechanism']['source_id']='health_risks:1'
    with pytest.raises(ValueError):parse_chains(sources,'',enums,lambda *a:value)


def test_unusable_source_is_recorded_without_inventing_chain():
    sources,enums,value=setup()
    value={'chains':[],'excluded':[{'source_id':sources[0]['source_id'],'reason':'机制不足待复核'}]}
    points,excluded=parse_chains(sources,'',enums,lambda *a:value)
    assert points==[] and len(excluded)==1


def test_multiple_independent_causes_in_one_source_create_two_rows():
    sources,enums,value=setup();sources[0]['content']+='食物过敏导致异常发酵。'
    chain=copy.deepcopy(value['chains'][0]);chain['cause']['text']='食物过敏';chain['mechanism']['text']='异常发酵';value['chains'].append(chain)
    points,_=parse_chains(sources,'',enums,lambda *a:value)
    assert len(points)==2 and points[0]['source_id']!=points[1]['source_id']


def test_repeated_symptom_statement_is_not_a_mechanism():
    sources,enums,value=setup()
    value['chains'][0]['mechanism']=copy.deepcopy(value['chains'][0]['cause'])
    with pytest.raises(ValueError):parse_chains(sources,'',enums,lambda *a:value)


def test_nutrition_condition_is_valid_grounded_judgment():
    sources,enums,value=setup()
    value['chains'][0]['verification']['supporting']=[{'source_id':'health_risks:0','text':'原文条件'}]
    points,_=parse_chains(sources,'',enums,lambda *a:value)
    assert points[0]['verification']['supporting']==['原文条件']
