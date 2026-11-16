"""Validate the scene contract without changing source text or verification structure."""
import json
from copy import deepcopy
import pytest
from services.health_solution_service import validate_scenario
from services.solution_knowledge_service import candidate_values
from vendor.chongxi_multi_model_mysql import run


def scenario():
    return {'species':'cat','breed':'all','life_stage':'adult','need':'digestive_health',
            'scenario_code':'stool_odor_normal_stool','scenario':' 便便臭但成形 ',
            'possible_causes':['底物变化','底物变化'],'solution':['记录换粮时间','观察粪便表现'],
            'verification':{'review_condition':'定期回顾','expected_result':'记录完整',
                            'failure_condition':'信息不足','next_action':'补充记录'}}


def test_scene_collector_preserves_raw_content_and_deduplicates():
    point=scenario()
    doc={'knowledge_base':'solution','question':'换粮后便便臭怎么处理？','solutions':[point,deepcopy(point)]}
    parsed=run.extract_json(json.dumps(doc,ensure_ascii=False))
    assert run.count_points(parsed)==1
    result=parsed['solutions'][0]
    assert {key:result[key] for key in point}==point
    candidate=candidate_values(result)
    assert candidate['scenario_data']==point
    assert candidate['steps']==[{'action':v,'reason':''} for v in point['solution']]
    assert candidate['evidence_status']=='pending_verification'


@pytest.mark.parametrize('field,value',[
    ('species','rabbit'),('need','消化'),('scenario_code',''),('scenario_code','x'*101),
    ('solution',[]),('solution','记录'),('possible_causes',[None]),
    ('verification',[]),('verification',{'review_condition':'回顾'}),
])
def test_invalid_scene_never_reaches_import(field,value):
    point=scenario();point[field]=value
    with pytest.raises(ValueError):validate_scenario(point)


def test_unspecified_scope_is_not_assumed_to_apply_to_all():
    point=scenario();del point['breed'];del point['life_stage']
    result=validate_scenario(point)
    assert result['breed']==result['life_stage']=='unspecified'


def test_verification_requires_exact_fields_and_nonempty_text():
    point=scenario();point['verification']['extra']='unexpected'
    with pytest.raises(ValueError):validate_scenario(point)
    point=scenario();point['verification']['next_action']=' '
    with pytest.raises(ValueError):validate_scenario(point)
