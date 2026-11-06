from unittest.mock import MagicMock, patch
import pytest
from services import catfood_standardization_service as service


@pytest.mark.parametrize('scenario', ['new', 'conflict', 'empty', 'draft_reuse'])
def test_no_conflict_new_formula_is_confirmed_without_promoting_conflicts(scenario):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    mapping = dict(source_id=529, brand_id=26, product_id=179, product_status='matched', raw_ingredient_composition='鸡肉')
    formula = dict(formula_id=332, formula_version=1, confirmation_status='confirmed')
    if scenario == 'new':
        cur.fetchone.side_effect = [mapping, None, {'next_version': 1}, formula]
        cur.fetchall.return_value = []
    elif scenario == 'conflict':
        cur.fetchone.side_effect = [mapping, None]
        cur.fetchall.return_value = [dict(formula, normalized_ingredients_json=['玉米'])]
    elif scenario == 'draft_reuse':
        cur.fetchone.side_effect = [mapping, dict(formula, confirmation_status='provisional')]
    else:
        cur.fetchone.side_effect = [mapping]
    ingredients = [] if scenario == 'empty' else ['鸡肉']
    with patch.object(service, '_connect', return_value=conn), \
         patch.object(service, 'init_standardization_db'), \
         patch.object(service, '_normalize_ingredients_with_noise', return_value=('鸡肉', ingredients, 'fp')), \
         patch.object(service, '_load_nutrition_signature', return_value={}), \
         patch.object(service, '_formula_nutrition_signature', return_value={}), \
         patch.object(service, 'nutrition_similarity', return_value={'comparable_count': 0, 'score': 1}), \
         patch.object(service, 'ordered_ingredient_similarity', return_value={'score': .1, 'top5_exact_ratio': 0}), \
         patch.object(service, '_link_guarantees_to_formula'), \
         patch.object(service, 'build_formula_feature_input', return_value={}) as build:
        result = service.standardize_formula({'source_id': 529})
    expected = {'new': 'matched', 'conflict': 'conflict', 'empty': 'pending', 'draft_reuse': 'provisional'}
    assert result['formula_status'] == expected[scenario]
    inserts = [call.args[0] for call in cur.execute.call_args_list if 'INSERT INTO' in call.args[0]]
    if scenario == 'new':
        assert len(inserts) == 1 and "'confirmed'" in inserts[0]
        build.assert_called_once_with(formula_id=332, apply=True)
    else:
        assert not inserts
    if scenario in {'conflict', 'empty'}:
        build.assert_not_called()
