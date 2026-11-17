from copy import deepcopy
import json
from unittest.mock import MagicMock

import pytest

from services import solution_scene_service as scenes
from services.solution_output_contract import adapt_document


def source(id=1, provider='qwen', **changes):
    point = {'species':'cat','breed':'unspecified','life_stage':'all',
             'need':'fecal_odor_management','scenario_code':'stool_odor_formed',
             'scenario':'便臭但成形', 'possible_causes':['原文原因'], 'solution':['原文步骤'],
             'verification':dict(zip(('review_condition','expected_result','failure_condition','next_action'),
                                     ('复核原文','预期原文','失败原文','后续原文')))}
    point.update(changes)
    return {'id':id,'batch_id':id,'task_id':9,'provider':provider,'model':provider+'-model',
            'question':'便臭怎么处理？','review_status':'pending','evidence_status':'supported',
            'source_urls':json.dumps(['https://example.com/'+provider]),
            'scenario_data':json.dumps(point,ensure_ascii=False),
            'raw_json':json.dumps({**point,'source_urls':[]},ensure_ascii=False)}


def test_same_scene_sources_merge_without_mixing_actions_or_verification():
    rows = [source(), source(2, 'deepseek', solution=['另一套步骤'])]
    groups = scenes.source_groups(rows)
    assert len(groups) == 1
    key, sources = next(iter(groups.items()))
    point = scenes.materialize(key, sources)
    assert point['source_count'] == point['model_count'] == 2
    assert point['steps'] == [{'action':'原文步骤','reason':''}]
    assert point['sources'][1]['scenario_data']['solution'] == ['另一套步骤']
    assert point['verification']['review_condition'] == '复核原文'
    assert point['evidence_status'] == 'pending_verification'
    assert len(point['source_urls']) == 2


@pytest.mark.parametrize('changes', [
    {'species':'dog'}, {'breed':'ragdoll'}, {'life_stage':'adult'},
    {'need':'weight_management'}, {'scenario_code':'stool_odor_soft'},
])
def test_each_dimension_is_part_of_the_grain(changes):
    assert len(scenes.source_groups([source(),source(2, **changes)])) == 2


def test_aliases_are_explicit_and_unknown_is_not_all():
    base = json.loads(source()['scenario_data'])
    assert scenes.identity(base)[2] == 'all_stages'
    other = {**base,'need':'fecal_odor_reduction'}
    assert scenes.identity(base) == scenes.identity(other)
    assert scenes.identity({**base,'life_stage':'unspecified'}) != scenes.identity(base)
    assert scenes.identity({**base,'breed':'all'}) != scenes.identity(base)
    # A cause-specific need is only reclassified under the explicitly registered question.
    other['need']='parasite_screening'
    assert scenes.identity(other)[3] == 'parasite_screening'
    assert scenes.identity(other,'猫咪便便臭怎么解决')[3] == 'fecal_odor_management'


def test_normalization_does_not_rewrite_original_source():
    row = source(need='fecal_odor_reduction', scenario_code='legacy_2df26dfd88342cf64fe0f5ebc4c0f37f')
    original = deepcopy(row)
    groups = scenes.source_groups([row])
    key = next(iter(groups))
    assert key[-1] == 'stool_odor_formed_recent_food_change'
    assert row == original
    assert groups[key][0]['scenario_data']['need'] == 'fecal_odor_reduction'


def test_new_explicit_fields_are_accepted_but_conflicts_rejected():
    point = json.loads(source()['scenario_data'])
    for a,b in zip(('species','breed','life_stage','need'),scenes.GRAIN[:4]):
        point[b] = point.pop(a)
    adapted = adapt_document({'solutions':[point]},question='问题')
    assert adapted['solutions'][0]['species'] == 'cat'
    point['species'] = 'dog'
    with pytest.raises(ValueError,match='冲突'):
        adapt_document({'solutions':[point]},question='问题')


def test_idempotent_refresh_preserves_reviewed_scene_and_rejects_changes():
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    key,sources=next(iter(scenes.source_groups([source()]).items()))
    point={**scenes.materialize(key,sources),'id':99,'review_status':'verified'}
    cur.fetchall.side_effect=[[source()],[point]]
    assert scenes.refresh(conn)['changed_count'] == 0
    cur.fetchall.side_effect=[[source(solution=['changed'])],[point]]
    with pytest.raises(ValueError,match='人工审核'):
        scenes.refresh(conn)
    assert not any(c.args[0].startswith(('INSERT','UPDATE','DELETE')) for c in cur.execute.call_args_list)


def test_legacy_generic_record_is_retained_in_detail_without_invented_scope():
    row=source();row['scenario_data']=None
    assert scenes.source_groups([row]) == {}
