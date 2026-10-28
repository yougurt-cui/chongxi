from unittest.mock import MagicMock, patch
import pytest
from main import create_app
from services import miniprogram_food_submission_service as service

BASE = {'id': 'own', 'status': 'active', 'review_status': 'pending', 'publish_status': 'unpublished',
        'recognition_status': 'processing', 'orchestrator_task_id': 'task', 'task_status': 'processing',
        'requested_run_id': 2, 'published_run_id': None, 'invalidated': 1}


def read(row=None, nodes=None):
    conn = MagicMock();conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = BASE.copy() if row is None else row
    cur.fetchall.return_value = nodes or []
    with patch.object(service, '_connect', return_value=conn), patch.object(service, 'init_food_submission_tables'), patch('services.formula_calculation_service.enabled', return_value=True):
        value = service.get_food_submission_status('owner', 'own')['item']
    assert cur.execute.call_args_list[0].args[1] == ('own', 'owner')
    assert cur.execute.call_count == 2
    return value


def test_light_status_running_and_version_changes():
    initial = read(nodes=[{'node_code': 'ocr_formula', 'node_status': 'running'}])
    assert initial['processing_stage'] == '读取包装文字' and initial['needs_refresh']
    assert not initial['result_available']
    row = {**BASE, 'task_status': 'done', 'published_run_id': 2, 'invalidated': 0, 'has_result': 1, 'result_stage': 'provisional'}
    provisional = read(row)
    official = read({**row, 'result_stage': 'official'})
    assert provisional['result_available'] and provisional['needs_refresh'] and not provisional['is_processing']
    assert official['revision'] != provisional['revision'] and not official['needs_refresh']
    assert official['result_id'] == provisional['result_id']
    assert not {'orchestrator_task_id', 'user_id', 'calculation_result', 'result_json'} & official.keys()


@pytest.mark.parametrize('change', [{'status': 'cancelled'}, {'review_status': 'rejected'}])
def test_cancelled_and_rejected_hide_result_and_stop_polling(change):
    value = read({**BASE, 'has_result': 1, 'published_run_id': 2, 'invalidated': 0, 'result_stage': 'official', **change})
    assert not value['result_available'] and not value['needs_refresh'] and not value['is_processing']


def test_invalidated_publication_does_not_claim_ready():
    value = read({**BASE, 'has_result': 1, 'published_run_id': 1, 'result_stage': 'official'})
    assert not value['result_available'] and value['result_updating'] and value['is_processing']


def test_status_requires_auth_and_ownership(monkeypatch):
    monkeypatch.setenv('CHONGXI_SESSION_SECRET', 'test-secret')
    client = create_app().test_client()
    assert client.get('/api/miniprogram/food-submissions/own/status').status_code == 401
    with patch('api.miniprogram_api._user_id_from_request', return_value='other'), patch('api.miniprogram_api.get_food_submission_status', side_effect=LookupError('食品添加记录不存在')):
        assert client.get('/api/miniprogram/food-submissions/own/status').status_code == 404
