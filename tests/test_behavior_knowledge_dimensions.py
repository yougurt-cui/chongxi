import pytest
from services.behavior_knowledge_dimensions import normalize_scope, values_for
from services.behavior_knowledge_schema import FIELDS, WRITE_FIELDS
from scripts.align_behavior_knowledge_dimensions import aligned_row

@pytest.fixture
def dictionary():
    return {'species':[{'species_code':sp} for sp in ('cat','dog')],
      'breeds':[{'species_code':sp,'breed_code':'unknown_'+sp,'breed_name':'未知'} for sp in ('cat','dog')]+[
          {'species_code':'cat','breed_code':'ragdoll','breed_name':'布偶猫','aliases_pipe':'布偶|Ragdoll'}],
      'stages':[{'species_code':sp,'stage_code':s} for sp in ('cat','dog') for s in ('adult','all_stages','unspecified')]+[
          {'species_code':'cat','stage_code':'kitten'},{'species_code':'dog','stage_code':'puppy'}]}

@pytest.mark.parametrize('raw,expected',[('未知','unspecified'),('未限定','unspecified'),('成年','adult'),('全阶段','all_stages')])
def test_stage_semantics(dictionary,raw,expected):
    result=normalize_scope({'species':'felis_catus','breed':'unknown','life_stage':raw},dictionary)
    assert (result['species_code'],result['breed_code'],result['life_stage_code'])==('cat','unknown_cat',expected)

def test_exact_dictionary_and_species_boundaries(dictionary):
    assert normalize_scope({'species':'cat','breed':'布偶','life_stage':'adult'},dictionary)['breed_code']=='ragdoll'
    result=normalize_scope({'species':'dog','breed':'布偶','life_stage':'kitten'},dictionary)
    assert result['breed_code'] is None and result['life_stage_code'] is None
    result=normalize_scope({'species':'unknown','breed':'unknown','life_stage':'adult'},dictionary)
    assert result['species_code'] is None and result['life_stage_code'] is None

def test_migration_preserves_identity_and_raw_content(dictionary):
    point={'species':'cat','breed':'未知','life_stage':'未限定','category':'other','evidence_status':'pending_review'}
    row={'id':3,'batch_id':540,'point_key':'a'*64,'point_data':point,'review_status':'verified','source_call_id':2}
    result=aligned_row(row,{'id':540,'provider':'qwen','model':'test'},dictionary)
    assert result['id']==3 and result['review_status']=='verified' and result['point_data']==point
    assert result['evidence_status']=='candidate' and result['source_count']==result['model_count']==1
    assert result['summary_meta']['raw_scope']['life_stage']=='未限定'
    assert result['sources'][0]['source_call_id']==2
    assert list(FIELDS)[:4]==['id','species_code','breed_code','life_stage_code']
    assert 'review_status' not in WRITE_FIELDS
