from datetime import datetime, timedelta
from copy import deepcopy
from unittest.mock import MagicMock, patch
import pytest
from scripts import merge_behavior_call_parse_records as merge


def run(run_id=1, **changes):
    start=datetime(2026,11,18,12)
    return {'id':run_id,'source_call_id':7,'source_hash':'a'*64,'parser_version':'v1',
        'status':'completed','attempts':2,'point_count':1,'error_message':None,
        'started_at':start,'finished_at':start+timedelta(minutes=1),**changes}


def test_merge_preserves_all_versions_and_attempts_without_modifying_raw_source():
    call={'id':7,'response_hash':'a'*64,'raw_response':'original','parse_status':'pending'}
    before=deepcopy(call)
    rows=[run(status='failed',error_message='old error'),run(2,parser_version='v2',attempts=3,
        finished_at=datetime(2026,11,18,13))]
    result=merge.merge_record(call,rows)
    assert result['parse_attempts']==5 and result['parser_version']=='v2'
    assert result['parse_status']=='completed' and result['point_count']==1
    assert len(result['parse_history'])==2 and result['parse_history'][0]['error_message']=='old error'
    assert call==before
    repeated=merge.merge_record({**call,**result},rows)
    assert repeated==result


def test_merge_keeps_more_recent_parse_result_and_adds_legacy_attempts_once():
    call={'response_hash':'a'*64,'parse_status':'failed','parse_error':'new error',
        'parse_attempts':1,'parser_version':'v3','parsed_at':datetime(2026,11,19),
        'parse_history':[{'attempt':1,'status':'failed'}]}
    result=merge.merge_record(call,[run()])
    assert result['parse_status']=='failed' and result['parse_error']=='new error'
    assert result['parser_version']=='v3' and result['parse_attempts']==3
    assert len(result['parse_history'])==2


def test_nonmatching_source_history_is_preserved_without_becoming_current_state():
    result=merge.merge_record({'response_hash':'b'*64},[run()])
    assert result['parse_status']=='pending' and result['parser_version'] is None
    assert len(result['parse_history'])==1


def test_running_parse_blocks_migration():
    with pytest.raises(ValueError,match='解析任务'):
        merge.merge_record({'response_hash':'a'*64},[run(status='running')])


def test_preview_does_not_alter_schema_or_create_backup(tmp_path):
    conn=MagicMock();conn.__enter__.return_value=conn
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value={'n':1}
    cur.fetchall.side_effect=[[run()],[{'id':7,'response_hash':'a'*64}]]
    with patch.object(merge.store,'connect',return_value=conn):
        result=merge.migrate(backup_root=tmp_path/'backup')
    assert result['parse_rows']==1 and result['merged_calls']==1
    assert not (tmp_path/'backup').exists()
    assert all(c.args[0].startswith('SELECT') for c in cur.execute.call_args_list)
    conn.commit.assert_not_called()
