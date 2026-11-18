import json
from unittest.mock import MagicMock, patch
import pytest
from scripts import parse_behavior_observations as script


def source():
    return {'species': 'cat', 'breed': 'unknown', 'life_stage': 'adult', 'observation': {
        'observation_code': 'vomiting_hairball', 'observation_name': '吐毛球',
        'normal_scenarios': [{'scenario': '原文', 'conditions': ['条件']}],
        'abnormal_scenarios': [{'boundary_id': 'B1', 'health_relations': [{'health_issue_name': '候选'}]}],
        'unmapped_health_issues': [{'health_issue_name': '未映射', 'status': 'pending_review'}]}}


def test_projection_retains_all_nested_scenarios_without_merging_or_interpretation():
    raw = source(); parsed = script.project(raw)
    assert parsed['species'] == 'cat' and parsed['breed'] == 'unknown'
    assert parsed['life_stage'] == 'adult' and parsed['observation_code'] == 'vomiting_hairball'
    for key in script.ARRAY_PATHS:
        assert parsed[key] == raw['observation'][key]
    assert parsed['normal_scenarios'][0]['conditions'] == ['条件']


def test_legacy_records_have_null_observation_fields_and_empty_arrays_stay_empty():
    assert script.project({'species':'cat','breed':'未限定'})['observation_name'] is None
    assert script.project({'species':'cat'})['normal_scenarios'] is None
    raw = source(); raw['observation']['normal_scenarios'] = []
    assert script.project(raw)['normal_scenarios'] == []


@pytest.mark.parametrize('change', [None, [], {'observation': []}, {'breed': 'x' * 121},
                                   {'observation': {'normal_scenarios': 'not-array'}}])
def test_malformed_or_truncating_data_is_rejected(change):
    with pytest.raises(ValueError):
        script.project(change)


def test_preview_never_writes_database_or_creates_backup(tmp_path):
    conn = MagicMock(); conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    row = {'id': 2, 'batch_id':539, 'species':'cat', 'review_status':'pending', 'point_data':json.dumps(source())}
    cur.fetchall.side_effect = [[{'Field':name, 'Extra':''} for name in row], [row], []]
    cur.fetchone.return_value = {'Create Table':'CREATE TABLE behavior_knowledge_point_detail (...)'}
    with patch.object(script.store, 'connect', return_value=conn):
        result = script.migrate(backup_root=tmp_path / 'backup')
    assert result['mode'] == 'preview' and result['observation_rows'] == 1
    assert len(result['new_columns']) == 7
    assert not (tmp_path / 'backup').exists()
    statements = [c.args[0] for c in cur.execute.call_args_list]
    assert all(s.startswith(('SELECT', 'SHOW')) for s in statements)
    conn.commit.assert_not_called()
