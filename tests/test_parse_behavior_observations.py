from unittest.mock import MagicMock, patch
import pytest
from scripts import parse_behavior_observations as script


def test_preview_never_writes_database_or_creates_backup(tmp_path):
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect=[[{'id':539,'task_id':539,'provider':'qwen','status':'success'}],[{'id':2,'batch_id':539}]]
    with patch.object(script.store,'connect',return_value=conn),patch.object(script.store,'ensure_database') as schema:
        result=script.migrate(backup_root=tmp_path/'backup',task_id=539)
    assert result['mode']=='preview' and result['batch_count']==1 and result['existing_points']==1
    assert result['call_table']=='behavior_model_call' and result['model_calls']==0
    assert not (tmp_path/'backup').exists()
    assert all(c.args[0].startswith('SELECT') for c in cur.execute.call_args_list)
    assert cur.execute.call_args_list[0].args[1]==[539]
    conn.commit.assert_not_called();schema.assert_not_called()


@pytest.mark.parametrize('kwargs',[{'task_id':0},{'task_id':-1},{'batch_id':'539'}])
def test_invalid_filters_are_rejected_before_connecting(kwargs):
    with patch.object(script.store,'connect') as connect:
        with pytest.raises(ValueError):script.migrate(**kwargs)
    connect.assert_not_called()
