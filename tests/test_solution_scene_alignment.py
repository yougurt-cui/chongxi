from unittest.mock import patch
import pytest
from services import solution_scene_service as scenes,solution_scene_alignment_service as alignment
from tests.test_solution_scene import source


def test_semantic_alignment_merges_different_codes_and_preserves_original_contexts():
    rows=[source(),source(2,'deepseek',scenario_code='another_code',scenario='近期换粮，成形便、精神食欲正常')]
    original=scenes.source_groups(rows)
    def group(scope,entries,generate):
        assert generate
        return {'alignment':{'groups':[{'scenario_codes':[e['scenario_code'] for e in entries],'reason':'相近观察背景，饮食诱因分别保留'}]},'meta':{'version':'test','model':'test'}}
    with patch.object(alignment,'align',side_effect=group):groups=alignment.regroup(original,generate=True)
    assert len(groups)==1
    sources=next(iter(groups.values()))
    assert len(sources)==2 and sources[0]['scenario_data']['scenario']!=sources[1]['scenario_data']['scenario']
    assert len({s['source_model'] for s in sources})==2
    assert all(s['scene_alignment']['original_scenario_codes']==['another_code','stool_odor_formed'] for s in sources)


def test_never_aligns_across_scope_or_without_cache_in_preview():
    rows=[source(),source(2,'deepseek',life_stage='kitten')]
    with patch.object(alignment,'align') as model:
        assert len(alignment.regroup(scenes.source_groups(rows),generate=True))==2
        model.assert_not_called()
    rows=[source(),source(2,'deepseek',scenario_code='other')]
    with patch.object(alignment,'align',return_value=None):
        assert len(alignment.regroup(scenes.source_groups(rows)))==2


@pytest.mark.parametrize('groups',[
 [{'scenario_codes':['a'],'reason':'missing'}],
 [{'scenario_codes':['a','a','b'],'reason':'duplicate'}],
 [{'scenario_codes':['a','fake'],'reason':'invented'}],
])
def test_alignment_requires_exact_coverage(groups):
    with pytest.raises(ValueError):alignment.validate({'groups':groups},{'a','b'})


def test_explicit_absence_is_not_a_positive_symptom_and_conflicting_observations_split():
    profile=alignment.observation_profile('成形、无腹泻/便秘、精神食欲良好')
    assert profile['formed'] and not profile['diarrhea'] and not profile['constipation']
    entries=[{'scenario_code':'a','sources':[{'scenario':'粪便成形，无腹泻、无呕吐、无血便'}]},
             {'scenario_code':'b','sources':[{'scenario':'反复腹泻和软便、水样便，近期捕猎'}]}]
    with pytest.raises(ValueError,match='前提冲突'):
        alignment.validate_compatibility({'groups':[{'scenario_codes':['a','b'],'reason':'错误对齐'}]},entries)


def test_normal_state_without_formed_word_still_cannot_merge_with_explicit_diarrhea():
    entries=[{'scenario_code':'a','sources':[{'scenario':'排便频率正常，无腹泻、无呕吐'}]},
             {'scenario_code':'b','sources':[{'scenario':'反复腹泻和胀气'}]}]
    with pytest.raises(ValueError,match='前提冲突'):
        alignment.validate_compatibility({'groups':[{'scenario_codes':['a','b'],'reason':'错误归组'}]},entries)
