from copy import deepcopy
from services import guardian_report_service as report
from services import guardian_health_analysis_service as health
from services.unified_product_profile_service import build_unified_profile, build_assessment
from test_guardian_health_analysis import portrait, knowledge


def test_new_market_cautions_keep_conclusion_without_repeated_disclaimer():
    _, cautions, _ = build_assessment(portrait(), {'weaknesses': ['消化支持'], 'rankings': {'消化支持': 9.1}})
    item = next(x for x in cautions if x['title'] == '消化支持')
    assert item['summary'] == '消化支持在当前市场中不突出'
    assert item['basis']['market_percentile'] == 9.1


def test_existing_profiles_clean_only_display_copy_and_preserve_stored_evidence():
    p = portrait()
    stored = build_unified_profile({'formula_id': 1, 'brand': '皇家', 'product_name': 'BK34',
                                   'structure_labels': [], 'market_rankings': {}, 'advantage_tags': [], 'weakness_tags': []}, p, {}, 'market')
    stored['standalone_assessment']['cautions'].append({'title': '消化支持', 'summary': '消化支持在当前市场中不突出，不代表不达标或一定不适合', 'basis': {'market_percentile': 9.1}})
    before = deepcopy(stored)
    result = report.product_evidence([{'formula_id': 1, 'portrait': p}], {1: stored})[0]
    assert result['cautions'][-1]['summary'] == '消化支持在当前市场中不突出'
    assert stored == before


def test_health_card_does_not_append_repeated_disclaimer():
    result = health.analyse_portrait(portrait(), knowledge())
    assert '不代表这款粮会引起疾病' not in health.report_text('皇家 BK34', result)
    assert result['presentation']['attentions']


def test_overview_keeps_evidence_note_out_of_conclusion():
    from services.guardian_health_summary_service import build_overview
    result = build_overview([{'label': '粮A', 'status': 'ready', 'presentation': {
        'advantages': [{'title': '纤维', 'summary': '纤维来源较丰富。'}], 'attentions': []}}], [], use_model=False)
    assert '纤维来源较丰富' in result['text']
    assert '不代表实际吃粮效果' not in result['text']
    assert '不代表实际吃粮效果' in result['note']


def test_comparison_does_not_append_generic_note_but_preserves_missing_data():
    from services.guardian_product_portrait_service import portrait_comparison_text
    comparison = {'status': 'comparable', 'common': [], 'complements': [], 'shortcomings': [],
                  'missing_dimensions': ['粗蛋白含量'],
                  'note': '画像比较描述配方特点，不代表实际健康效果；互补不表示建议混喂。'}
    text = portrait_comparison_text(comparison)
    assert '粗蛋白含量' in text
    assert '不代表实际健康效果' not in text
    comparison.update(status='unavailable', note='资料或计算版本不一致，暂不比较。')
    assert portrait_comparison_text(comparison) == comparison['note']
