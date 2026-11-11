import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from services import miniprogram_food_submission_service as service


def sync(output, nutrients, *, persist=False):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = nutrients
    task = {'task_status': 'done', 'outputs': {'ocr_formula': output}, 'nodes': []}
    with patch.object(service, '_connect', return_value=conn), \
            patch.object(service, 'get_orchestrator_task', return_value=task), \
            patch('services.formula_calculation_service.enabled', return_value=False):
        row = service._sync_recognition({
            'id': 'submission', 'status': 'active', 'orchestrator_task_id': 'task',
            'review_status': 'pending', 'guarantee_json': '{}',
        }, persist=persist)
    return json.loads(row['guarantee_json']), cursor, conn


def test_old_submission_loads_nutrients_for_its_own_upload_without_writing():
    guarantee, cursor, conn = sync({'source_id': 549}, [
        {'metric_name': '粗蛋白', 'operator_symbol': '>=', 'metric_value': Decimal('29.00'),
         'metric_unit': '%', 'basis': '干物质'},
        {'metric_name': '水分', 'operator_symbol': '<=', 'metric_value': Decimal('1.00'),
         'metric_unit': '%', 'basis': None},
        {'metric_name': '缺失项', 'metric_value': None},
    ])
    assert guarantee == {'粗蛋白': '≥29.00%（干物质）', '水分': '≤1.00%'}
    assert cursor.execute.call_args_list[0].args[1] == (549,)
    assert len(cursor.execute.call_args_list) == 1
    conn.commit.assert_not_called()


def test_explicit_task_guarantees_are_preserved():
    guarantee, cursor, _ = sync({'source_id': 549, 'guarantee': {'粗蛋白': '≥29%'}}, [])
    assert guarantee == {'粗蛋白': '≥29%'}
    cursor.execute.assert_not_called()


def test_missing_nutrients_remain_empty_instead_of_using_another_formula():
    guarantee, cursor, _ = sync({'source_id': 549}, [])
    assert guarantee == {}
    assert cursor.execute.call_args.args[1] == (549,)
    guarantee, cursor, _ = sync({}, [])
    assert guarantee == {}
    cursor.execute.assert_not_called()
