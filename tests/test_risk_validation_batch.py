from unittest.mock import MagicMock
from services import orchestrator_service as service


def test_risk_validation_reads_only_requested_calculation_batch():
    cursor = MagicMock()
    cursor.fetchall.return_value = [{'score_model_version': 'current'}]
    rows = service._fetch_risk_result_rows(cursor, '皇家||EP42', batch_id='calc-501')
    sql, params = cursor.execute.call_args.args
    assert 'batch_id = %s' in sql
    assert params == ('皇家||EP42', 'calc-501')
    assert rows == [{'score_model_version': 'current'}]


def test_legacy_validation_without_batch_keeps_existing_lookup():
    cursor = MagicMock()
    service._fetch_risk_result_rows(cursor, '皇家||EP42')
    sql, params = cursor.execute.call_args.args
    assert 'batch_id = %s' not in sql
    assert params == ('皇家||EP42',)
