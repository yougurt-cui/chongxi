"""Publication contracts; no database or score subprocess required."""
import json
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from services import formula_calculation_service as calc
from services.guardian_product_portrait_service import _published_or_legacy, compare_portraits


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv('CHONGXI_VERSIONED_CALCULATION', '1')


def connection():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    return conn, cur


def test_identity_and_nutritional_evidence_in_cache_key():
    row = {'normalized_ingredients_json': ['鸡肉', '鱼油'], 'nutrition_json': {'protein': {'value': 30, 'unit': '%', 'operator': '>='}}, 'standard_brand_name': 'A'}
    assert calc.input_fingerprint(row) == calc.input_fingerprint({**row, 'nutrition_json': json.dumps(row['nutrition_json']), 'source_id': 2, 'result_stage': 'official'})
    assert calc.input_fingerprint(row) != calc.input_fingerprint({**row, 'result_stage': 'provisional', 'quality_flags_json': '["ingredient_proxy"]'})
    for patch_row in ({'standard_brand_name': 'B'}, {'normalized_ingredients_json': ['鱼油', '鸡肉']}, {'nutrition_json': {'protein': {'value': 30, 'unit': '%', 'operator': '='}}}):
        assert calc.input_fingerprint(row) != calc.input_fingerprint({**row, **patch_row})


@pytest.mark.parametrize('value, expected', [('普通文本', ['普通文本']), ('["a","b"]', ['a','b']), (None, []), ('', []), ('{"a":1}', [])])
def test_mixed_legacy_risk_tags(value, expected):
    assert calc.reason_tags(value) == expected


def test_publication_rejects_invalid_scores_in_features_labels_and_quality_flags():
    conn, cur = connection()
    cur.fetchone.return_value = {'batch_id': 'batch'}
    cur.fetchall.return_value = [
        {'score_model_version': model, 'reference_pool_version': 'pool',
         'current_pool_risk_level': 'low', 'main_reason_tags': '[]',
         'support_reason_tags': '["支持标签"]', 'fat_detail_tags': '脂肪标签'}
        for model in ('SOFT_STOOL_V1', 'BLACK_CHIN_V1')
    ]
    row = {'id': 5, 'formula_id': 1, 'fiber_score': 5.39, 'protein_score': .4}
    with patch.object(calc.pymysql, 'connect', return_value=conn), patch(
        'services.product_portrait_summary_service._load_feature_rows', return_value=[row]
    ):
        bundle = calc.capture_bundle(1, 'batch', {'input_json': '{}', 'input_fingerprint': 'input', 'dependency_fingerprint': 'rules'})
    assert bundle['features']['fiber_score'] is None
    assert bundle['features']['protein_score'] == .4
    assert bundle['portrait']['dimensions']['fiber']['score'] is None
    assert bundle['portrait']['dimensions']['fiber']['level'] == 'missing'
    assert '高纤维托底' not in bundle['portrait']['summary']
    assert '纤维支持资料不足' in bundle['portrait']['summary']
    assert bundle['quality_flags'] == ['invalid_score:fiber_score']
    assert row['fiber_score'] == 5.39
    assert all(r['support_reason_tags'] == ['支持标签'] and r['fat_detail_tags'] == ['脂肪标签']
               for r in bundle['risks'])


@pytest.mark.parametrize('value', [-.01, 5.39, float('nan'), float('inf'), 'invalid'])
def test_publication_score_validation_handles_invalid_and_boundary_values(value):
    from services.product_portrait_summary_service import validated_feature_row
    row = {'fiber_score': value, 'carb_score': 0, 'prebiotic_score': 5, 'fat_score': 1}
    result = validated_feature_row(row)
    assert result['fiber_score'] is None
    assert result['protein_score'] is None
    assert result['carb_score'] == 0 and result['prebiotic_score'] == 5 and result['fat_score'] == 1


def test_snapshot_nutrition_preserves_guarantee_and_missing():
    values = {'a': {'metric_name': '粗蛋白', 'value': '32', 'unit': '％', 'operator': '≥', 'basis': '原物质'},
              'b': {'metric_name': '水分', 'value': '101', 'unit': '%'},
              'c': {'metric_name': '粗脂肪', 'value': 'NaN', 'unit': '%'}}
    result = calc.snapshot_nutrition({'nutrition_json': values})
    assert result['粗蛋白']['operator'] == '>='
    assert result['粗蛋白']['basis'] == '原物质'
    assert result['粗蛋白']['value'] == 32
    assert set(result) == {'粗蛋白'}


def test_duplicate_conflicting_nutrition_is_not_invented():
    a = {'metric_name': '粗蛋白', 'value': 30, 'unit': '%'}
    result = calc.snapshot_nutrition({'nutrition_json': {'a': a, 'b': {**a, 'value': 31}, 'c': a}})
    assert result['粗蛋白']['value'] is None


def test_managed_unpublished_never_falls_back_to_live_rows():
    legacy = MagicMock(return_value={2: {'legacy': True}})
    with patch.object(calc, 'published', return_value={1: {'result_id': None}}):
        result = _published_or_legacy([1, 2], 'portrait', legacy)
    legacy.assert_called_once_with([2])
    assert 1 not in result


def test_request_pins_bundle_and_invalidations_hide_conclusions(enabled):
    conn, cur = connection()
    cur.fetchall.return_value = [{'formula_id': 1, 'invalidated': 1, 'requested_run_id': 4, 'published_run_id': 3,
        'result_stage': 'official', 'result_json': json.dumps({'portrait': {'score': 99}}), 'updated_at': None,
        'input_json': json.dumps({'nutrition_json': {}}), 'dependency_fingerprint': 'new'}]
    app = Flask(__name__)
    with app.test_request_context(), patch.object(calc, 'connect', return_value=conn):
        a = calc.published([1])[1]
        b = calc.published([1])[1]
    assert a is b and 'portrait' not in a and a['invalidated']
    assert cur.fetchall.call_count == 1


def test_stale_input_rejected_before_capture():
    with patch.object(calc, 'task_run', return_value={'formula_id': 1, 'input_fingerprint': 'old'}), patch.object(calc, 'read_input', return_value={}), patch.object(calc, 'capture_bundle') as capture:
        with pytest.raises(calc.CalculationSuperseded):
            calc.publish('task', 'batch')
    capture.assert_not_called()


@pytest.mark.parametrize('task,node,error', [
    ({'task_status': 'cancelled'}, {}, '撤销'),
    ({'task_status': 'processing'}, {'node_status': 'running', 'claim_token': 'new-owner'}, '重置'),
])
def test_cancelled_or_replaced_owner_cannot_publish(task, node, error):
    conn, cur = connection();cur.fetchone.side_effect = [task, node]
    run = {'id': 1, 'formula_id': 1, 'input_fingerprint': calc.input_fingerprint({}), 'dependency_fingerprint': 'rules'}
    with patch.object(calc, 'task_run', return_value=run), patch.object(calc, 'read_input', return_value={}), patch.object(calc, 'dependency_fingerprint', return_value='rules'), patch.object(calc, 'capture_bundle', return_value={}), patch.object(calc, 'connect', return_value=conn):
        with pytest.raises(ValueError, match=error):
            calc.publish('task', 'batch', claim_token='old-owner')
    conn.commit.assert_not_called()


def test_provisional_identity_cannot_promote():
    with patch.object(calc, 'read_input', return_value={'result_stage': 'provisional'}), patch.object(calc, 'connect') as connect:
        assert not calc.promote(1)
    connect.assert_not_called()


def test_incompatible_rules_refuse_comparison():
    from services.product_portrait_summary_service import PORTRAIT_VERSION, DEFAULT_FEATURE_VERSION
    p = {'formula_id': 1, 'feature_version': DEFAULT_FEATURE_VERSION, 'portrait_version': PORTRAIT_VERSION, 'calculation_version': 'a'}
    assert compare_portraits(p, {**p, 'calculation_version': 'b'})['status'] == 'unavailable'


def test_formula_lock_is_exclusive_and_released():
    conn, cur = connection();cur.fetchone.return_value = {'acquired': 1}
    with patch.object(calc, 'connect', return_value=conn):
        with pytest.raises(RuntimeError):
            with calc.formula_lock(1) as acquired:
                assert acquired
                raise RuntimeError('failure')
    assert 'RELEASE_LOCK' in cur.execute.call_args.args[0]


def test_reference_change_rebinds_risks_without_restarting_scores():
    previous = {'scoring': 'same-maths', 'risk_reference': 'old-pool'}
    current = {'scoring': 'same-maths', 'risk_reference': 'new-pool'}
    snapshot = {'_dependency_components': previous, 'standard_brand_name': 'A'}
    run = {'id': 1, 'formula_id': 3, 'input_json': json.dumps(snapshot), 'dependency_fingerprint': calc.digest(previous)}
    with patch.object(calc, 'task_run', return_value=run), patch.object(calc, 'dependency_components', return_value=current), patch.object(calc, 'begin', return_value={'calculation_run_id': 2}) as begin:
        result = calc.prepare_risks('task')
    assert result['risk_dependencies_refreshed'] and result['calculation_run_id'] == 2
    begin.assert_called_once_with('task', 3, _input=snapshot, _dependencies=current, _expected_run_id=1)


def test_scoring_rule_change_cannot_relabel_old_scores():
    previous = {'scoring': 'old-maths', 'risk_reference': 'old-pool'}
    run = {'id': 1, 'formula_id': 3, 'input_json': json.dumps({'_dependency_components': previous}), 'dependency_fingerprint': calc.digest(previous)}
    with patch.object(calc, 'task_run', return_value=run), patch.object(calc, 'dependency_components', return_value={'scoring': 'new-maths', 'risk_reference': 'new-pool'}), patch.object(calc, 'begin') as begin:
        with pytest.raises(calc.CalculationSuperseded, match='基础评分规则'):
            calc.prepare_risks('task')
    begin.assert_not_called()


def test_unchanged_reference_keeps_run():
    current = {'scoring': 'same', 'risk_reference': 'same'}
    with patch.object(calc, 'task_run', return_value={'input_json': '{}', 'dependency_fingerprint': calc.digest(current)}), patch.object(calc, 'dependency_components', return_value=current), patch.object(calc, 'begin') as begin:
        assert not calc.prepare_risks('task')['risk_dependencies_refreshed']
    begin.assert_not_called()


def test_reference_rebind_cannot_replace_newer_requested_run():
    conn, cur = connection()
    cur.fetchone.side_effect = [{'task_status': 'processing'}, {'run_id': 1, 'requested_run_id': 2}]
    with patch.object(calc, 'connect', return_value=conn):
        with pytest.raises(calc.CalculationSuperseded, match='替代'):
            calc.begin('task', 3, _input={}, _dependencies={'scoring': 'same', 'risk_reference': 'new'}, _expected_run_id=1)
    conn.commit.assert_not_called()


def test_reference_rebind_rejects_concurrent_input_edit():
    conn, cur = connection()
    cur.fetchone.side_effect = [{'task_status': 'processing'}, {'run_id': 1, 'requested_run_id': 1}, {'standard_brand_name': 'changed'}]
    with patch.object(calc, 'connect', return_value=conn):
        with pytest.raises(calc.CalculationSuperseded, match='输入'):
            calc.begin('task', 3, _input={}, _dependencies={'scoring': 'same', 'risk_reference': 'new'}, _expected_run_id=1)
    conn.commit.assert_not_called()
