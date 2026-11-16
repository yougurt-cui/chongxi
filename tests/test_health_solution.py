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


def test_legacy_template_response_uses_task_metadata_and_exact_scenario_identity():
    point=scenario();del point['scenario_code'];point['species']='feline'
    point['evidence_status']='veterinary_consensus_pending_review'
    raw=json.dumps({'solutions':[point]},ensure_ascii=False)
    parsed=run.extract_json(raw,knowledge_base='solution',question='原始问题')
    result=parsed['solutions'][0]
    assert parsed['question']=='原始问题' and parsed['knowledge_base']=='solution'
    assert result['species']=='cat' and result['evidence_status']=='pending_verification'
    assert result['scenario_code'].startswith('legacy_')
    assert result['scenario_code']==run.extract_json(raw,knowledge_base='solution',question='另一问题')['solutions'][0]['scenario_code']
    assert result['scenario']==point['scenario'] and result['solution']==point['solution']
    assert json.loads(raw)['solutions'][0]['species']=='feline'
    point['scenario']='另一种观测场景'
    different=run.extract_json(json.dumps({'solutions':[point]}),knowledge_base='solution',question='原始问题')
    assert different['solutions'][0]['scenario_code']!=result['scenario_code']


def test_solution_context_does_not_accept_other_library_or_invalid_actions():
    with pytest.raises(ValueError,match='不一致'):
        run.extract_json('{"knowledge_base":"health","solutions":[]}',knowledge_base='solution',question='test')
    point=scenario();point['solution']=[]
    with pytest.raises(ValueError):
        run.extract_json(json.dumps({'solutions':[point]}),knowledge_base='solution',question='test')


def test_malformed_outer_json_never_falls_back_to_verification_or_other_child():
    raw='前言\n```json\n{"solutions":[{"scenario":"气味似乎\"跟着猫走\"","verification":{"next_action":"观察"}}'
    with pytest.raises(ValueError,match='JSON格式错误'):
        run.extract_json(raw,knowledge_base='solution',question='test')
    valid='前言\n```json\n'+json.dumps({'solutions':[scenario()]})+'\n```'
    assert run.extract_json(valid,knowledge_base='solution',question='test')['solutions']


def test_runtime_contract_always_follows_custom_template_and_renders_parameters():
    from services.solution_output_contract import render_prompt
    prompt=render_prompt('只输出solutions；问题{{question}} 种类{{species}} 场景数{{scenario_count}}','猫便臭')
    assert '{{' not in prompt
    assert prompt.index('只输出solutions')<prompt.index('解决方案入库输出约束')
    assert 'scenario_code' in prompt and 'knowledge_base="solution"' in prompt
    assert prompt.endswith('"猫便臭"')


def test_retry_saved_solution_response_updates_parsed_result_without_changing_raw():
    from unittest.mock import MagicMock,patch
    from services import model_knowledge_import_service as store
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    raw=json.dumps({'solutions':[scenario()]})
    cur.fetchone.return_value={'knowledge_base':'solution','status':'parse_failed','question':'问题','raw_response':raw}
    with patch.object(store,'connect',return_value=conn):store.reparse_solution_batch(5)
    updates=[c for c in cur.execute.call_args_list if c.args[0].startswith('UPDATE')]
    assert len(updates)==1
    assert 'raw_response=' not in updates[0].args[0]
    assert json.loads(updates[0].args[1][0])['question']=='问题'
    conn.commit.assert_called_once()


def test_retry_invalid_json_retains_failure_and_exposes_true_error():
    from unittest.mock import MagicMock,patch
    from services import model_knowledge_import_service as store
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value={'knowledge_base':'solution','status':'parse_failed','question':'问题','raw_response':'{"solutions": ['}
    with patch.object(store,'connect',return_value=conn),pytest.raises(ValueError,match='JSON格式错误'):
        store.reparse_solution_batch(5)
    updates=[c for c in cur.execute.call_args_list if c.args[0].startswith('UPDATE')]
    assert len(updates)==1 and 'error_message=' in updates[0].args[0]
    assert 'status=' not in updates[0].args[0]
    conn.commit.assert_called_once()


def test_manual_retry_processes_recovered_solution_and_reports_invalid_batch():
    from unittest.mock import MagicMock,patch
    from services import model_knowledge_import_service as store
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[{'id':5,'status':'parse_failed','knowledge_base':'solution'},
                              {'id':6,'status':'parse_failed','knowledge_base':'solution'},
                              {'id':7,'status':'parse_failed','knowledge_base':'health'}]
    with patch.object(store,'ensure_database'),patch.object(store,'connect',return_value=conn), \
         patch.object(store,'reparse_solution_batch',side_effect=[None,ValueError('JSON格式错误')]) as reparse, \
         patch.object(store,'process_batch_pipeline',return_value={'status':'completed'}) as pipeline:
        result=store.import_task(1)
    assert reparse.call_count==2
    pipeline.assert_called_once_with(5)
    assert result['errors']==[{'batch_id':6,'message':'JSON格式错误'}]


def test_provider_applies_solution_context_to_native_search_response():
    from unittest.mock import patch
    point=scenario();del point['scenario_code'];point['species']='feline'
    with patch('services.model_native_search_service.call',return_value=(json.dumps({'solutions':[point]}),{},{})):
        result=run.call_provider(run.Provider('qwen','test','','test'),'prompt',web_search=True,
                                 knowledge_base='solution',question='问题')
    assert result['status']=='success'
    assert result['parsed']['question']=='问题'
    assert result['parsed']['solutions'][0]['species']=='cat'


def test_unescaped_paired_chinese_quotes_repair_preserves_text_and_records_offsets():
    point=scenario();point['scenario']='气味似乎"跟着猫走"、不局限于猫砂盆。'
    good=json.dumps({'solutions':[point]},ensure_ascii=False)
    broken=good.replace('\\"跟着猫走\\"','"跟着猫走"')
    metadata={}
    parsed=run.extract_json('前言\n```json\n'+broken+'\n```',knowledge_base='solution',question='问题',parse_metadata=metadata)
    assert parsed['solutions'][0]['scenario']==point['scenario']
    assert parsed['solutions'][0]['solution']==point['solution']
    assert metadata['json_format_repairs'][0]['type']=='escape_paired_cjk_quotes'
    with pytest.raises(ValueError,match='JSON格式错误'):run.extract_json(broken)


@pytest.mark.parametrize('raw',[
    '{"solutions": [{"scenario": "观察" "solution": []}]}',
    '{"solutions": [{"scenario": "观察"}',
    '{"solutions": [{"scenario": "气味似乎"跟着猫走"、未结束',
])
def test_quote_repair_does_not_accept_missing_commas_or_truncation(raw):
    with pytest.raises(ValueError,match='JSON格式错误'):
        run.extract_json(raw,knowledge_base='solution',question='问题')
