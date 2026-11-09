import unittest

from services.fiber_science_materialization_service import build_science_payload
from vendor.feature_score_pipeline.scripts.fiber_remark_score import compute_scores


class FiberSourcePositionTest(unittest.TestCase):
    def payload(self, raw, standard, position, text, category='fiber'):
        return build_science_payload(
            [{'position': position, 'raw_name': raw, 'standard_name': standard,
              'standard_ingredient_id': 'A'}],
            {'A': {'science_status': 'active', 'nutrition_category': category,
                   'domain_attributes_json': {'fiber_solubility': 'soluble',
                                              'fermentability': 'high',
                                              'prebiotic_functions': ['microbiome_feed']}}},
            text)['ingredient_feature_json']

    def test_standard_alias_uses_original_position(self):
        text = '、'.join(['肉'] * 14 + ['车前子(0.5%)'])
        feature = self.payload('车前子', '圆苞车前子壳', 15, text)
        result = compute_scores(text, feature)
        self.assertEqual(result['matched_ingredients_json'][0]['rank'], 15)
        self.assertEqual(result['matched_ingredients_json'][0]['weight'], 0.2)
        self.assertEqual(result['matched_ingredients_json'][0]['matched_ingredient'], '车前子')

    def test_compound_child_preserves_parent_and_caps_weight(self):
        text = '鲜鸡肉86.1%、冻干生骨肉6%(冻鸡肉、菊苣菊糖、蜂蜜(麦卢卡蜂蜜)100mg/kg)、纤维素'
        feature = self.payload('菊苣菊糖', '菊苣根粉', 3, text, 'prebiotic')
        detail = compute_scores(text, feature)['score_detail_json'][0]
        self.assertEqual(detail['rank'], 2)
        self.assertEqual(detail['weight'], 0.06)
        self.assertEqual(detail['parent_fraction'], 0.06)
        self.assertEqual(detail['position_origin'], 'compound_parent_cap')

    def test_following_ingredient_uses_top_level_order(self):
        text = '肉、冻干6%(鸡肉、菊糖、蜂蜜)、油、米、蛋、鱼、肝、纤维素'
        feature = self.payload('纤维素', '纤维素', 10, text)
        result = compute_scores(text, feature)['matched_ingredients_json'][0]
        self.assertEqual(result['rank'], 8)
        self.assertEqual(result['weight'], 0.6)

    def test_unknown_compound_amount_requires_review(self):
        with self.assertRaisesRegex(ValueError, '父原料比例'):
            self.payload('菊糖', '菊苣根粉', 3, '肉、冻干(鸡肉、菊糖)')

    def test_legacy_unmatched_does_not_silently_score(self):
        with self.assertRaisesRegex(ValueError, '未匹配'):
            compute_scores('车前子(0.5%)', {'ingredient_tag_detail': {'圆苞车前子壳': {}}})


if __name__ == '__main__':
    unittest.main()
