from unittest.mock import MagicMock, patch
from services import guardian_report_service as report


def db(rows):
    conn = MagicMock()
    conn.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = rows
    return conn


def product(brand='添赐力'):
    return {'community_brand': brand, 'cautions': [{'dimension': 'fiber', 'title': '纤维支持'}]}


def test_unknown_brand_does_not_retrieve_generic_experiences():
    with patch.object(report.knowledge, 'retrieve') as retrieve, patch.object(report, '_connect') as connect:
        result = report.community_evidence({}, [{'brand': None, 'cautions': []}])
    retrieve.assert_not_called()
    connect.assert_not_called()
    assert result['status'] == 'no_match'


def test_other_brands_and_unrelated_topics_are_excluded_even_in_same_guide():
    rows = [{'id': 1, 'content': '其他牌软便；添赐力饮水多；添赐力出现便秘；其他牌导致尿闭',
             'source_knowledge_ids': [9], 'evidence_status': '待验证'}]
    with patch.object(report, '_connect', return_value=db([])), \
         patch.object(report.knowledge, 'retrieve', return_value={'status': 'matched', 'items': rows}) as retrieve:
        result = report.community_evidence({'animal_type': 'cat'}, [product()])
    item, = result['items']
    assert item['text'] == '添赐力出现便秘'
    assert item['matched_cautions'] == ['纤维支持']
    assert item['source']['brand'] == '添赐力'
    assert retrieve.call_args.args[0]['anchors'] == ['添赐力']


def test_brand_clues_are_narrowed_by_caution_topics():
    rows = [{'brand': '添赐力', 'secondary_symptom': symptom, 'sample_count': 3, 'improved': 1, 'worsened': 2}
            for symptom in ['软便', '尿闭']]
    with patch.object(report, '_connect', return_value=db(rows)), \
         patch.object(report.knowledge, 'retrieve', return_value={'status': 'no_match', 'items': []}):
        result = report.community_evidence({}, [product()])
    assert len(result['items']) == 1
    assert result['items'][0]['source']['symptom'] == '软便'
    assert '不能归因于本配方' in result['items'][0]['text']


def test_no_brand_match_stays_empty_without_generic_fallback():
    with patch.object(report, '_connect', return_value=db([])), \
         patch.object(report.knowledge, 'retrieve', return_value={'status': 'matched', 'items': [
             {'id': 1, 'content': '其他品牌软便的经验'}]}):
        result = report.community_evidence({}, [product()])
    assert result['status'] == 'no_match'
    assert not result['items']
    assert '暂无' in result['note']
