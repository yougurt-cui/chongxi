from unittest.mock import patch

from services import guardian_factor_evidence_service as factor


def test_managed_pending_never_reads_working_risk_tables():
    with patch('services.formula_calculation_service.published', return_value={1: {'invalidated': True}}), \
            patch.object(factor, '_load_legacy_profiles', return_value={2: {'formula_id': 2}}) as legacy:
        assert factor.load_profiles([1, 2]) == {2: {'formula_id': 2}}
        legacy.assert_called_once_with([2])


def test_published_factor_tags_are_pinned_without_live_percentiles():
    bundle = {'result_id': '17', 'calculation_version': 'rules-a', 'risks': [
        {'kind': 'soft_stool', 'model_version': 'SOFT_STOOL_v1',
         'relative_risk_level': 'high', 'reason_tags': ['碳水结构压力偏高'],
         'support_reason_tags': ['菌群代谢支持不足'], 'fat_detail_tags': ['脂肪来源较复杂']}]}
    with patch('services.formula_calculation_service.published', return_value={1: bundle}), \
            patch.object(factor, '_load_legacy_profiles', return_value={}) as legacy, \
            patch.object(factor, 'load_community_evidence', return_value=[]), \
            patch.object(factor, 'load_pool_positions') as pool:
        profile = factor.load_profiles([1])[1]
        legacy.assert_called_once_with([])
        pool.assert_not_called()
        assert profile['result_id'] == '17'
        assert profile['factors']['carb']['structural'] == ['碳水结构压力偏高']
        assert profile['factors']['fat']['structural'] == ['脂肪来源较复杂']
        assert any('菌群代谢支持不足' in v['function'] for v in profile['factors'].values())
        assert all(v is None for v in profile['positions'].values())
        comparison = factor.compare_profiles({**profile, 'formula_id': 2}, profile)
        assert comparison['status'] == 'partial'
        assert '因子到症状' in factor.factor_comparison_text(comparison, profile, profile)


def test_mixed_versions_cannot_create_factor_comparison():
    assert factor.compare_profiles({'formula_id': 1, 'calculation_version': 'v1'},
                                   {'formula_id': 2})['status'] == 'unavailable'
    assert factor.compare_profiles({'formula_id': 1, 'calculation_version': 'v1'},
                                   {'formula_id': 2, 'calculation_version': 'v2'})['status'] == 'unavailable'
