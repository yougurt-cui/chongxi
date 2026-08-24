import unittest

from services.catfood_standardization_service import (
    _brand_input_candidates,
    _nutrition_completeness,
    _merge_product_candidate_lineage,
    _merge_json_lists,
    _preferred_value,
    ingredient_similarity,
    nutrition_similarity,
    normalize_ingredients,
    normalize_name,
    ordered_ingredient_similarity,
)


class CatfoodStandardizationServiceTest(unittest.TestCase):
    def test_page_brand_input_precedes_ocr_brand_fallback(self):
        candidates = _brand_input_candidates(
            {"brand_name": "肉垫"},
            {"raw_brand_name": "rf45低敏茶花鸡", "raw_product_name": "0702182336"},
        )
        self.assertEqual(candidates[0], "肉垫")
        self.assertIn("rf45低敏茶花鸡", candidates)

    def test_ocr_brand_is_used_when_page_brand_is_empty(self):
        candidates = _brand_input_candidates(
            {"brand_name": ""},
            {"raw_brand_name": "鲜朗", "raw_product_name": "成猫粮"},
        )
        self.assertEqual(candidates[0], "鲜朗")

    def test_nutrition_completeness_uses_core_guarantee_metrics(self):
        nutrition = {
            "protein": {"metric_name": "粗蛋白"},
            "fat": {"metric_name": "粗脂肪"},
            "fiber": {"metric_name": "粗纤维"},
            "calcium": {"metric_name": "钙"},
        }
        self.assertEqual(_nutrition_completeness(nutrition), 0.57143)

    def test_name_normalization(self):
        self.assertEqual(normalize_name("N&D 南瓜系列"), "n&d南瓜系列")

    def test_ingredient_synonyms_produce_same_fingerprint(self):
        left = normalize_ingredients("鲜鸡肉60%、冻鸡胸肉20%、鲑鱼5%、添加剂组成：牛磺酸")
        right = normalize_ingredients("鲜鸡肉60%，鲜鸡胸肉20%，三文鱼5%")
        self.assertEqual(left[1], right[1])
        self.assertEqual(left[2], right[2])

    def test_ingredient_ocr_ui_prefix_does_not_change_fingerprint(self):
        ocr = normalize_ingredients("直播讲解鲜鸡肉75.5%、鲜鸭肉11%、鲜鸡肝5%")
        formula = normalize_ingredients("鲜鸡肉75.5%、鲜鸭肉11%、鲜鸡肝5%")
        self.assertEqual(ocr[1], formula[1])
        self.assertEqual(ocr[2], formula[2])

    def test_ingredient_noise_pool_names_are_removed_before_fingerprinting(self):
        normalized, ingredients, fingerprint = normalize_ingredients(
            "鲜鸡肉、产品成份、鸡油",
            {normalize_name("产品成份")},
        )
        expected = normalize_ingredients("鲜鸡肉、鸡油")
        self.assertEqual(normalized, expected[0])
        self.assertEqual(ingredients, expected[1])
        self.assertEqual(fingerprint, expected[2])

    def test_variant_product_analysis_heading_ends_ingredient_text(self):
        _, ingredients, _ = normalize_ingredients(
            "鲜鸡肉、鸡油、产品成份分析保证值、粗蛋白质≥38%"
        )
        self.assertEqual(ingredients, ["鲜鸡肉", "鸡油"])

    def test_grouped_ingredient_headers_expand_to_children(self):
        _, ingredients, _ = normalize_ingredients(
            "鱼类等水生生物及其制品43%(金枪鱼、酶解沙丁鱼、酶解鳀鱼、深海白鱼粉、酶解三文鱼、深海三文鱼油)、"
            "肉类及其制品42%、酶解鸡肉、鸡肉粉、鸡肉、鸡油、牛油、酶解鸡肝)、"
            "果蔬类籽实及其制品(紫薯、马铃薯、南瓜、苹果、梨、蔓越莓、菊苣根粉)"
        )
        self.assertNotIn("鱼类等水生生物及其制品", ingredients)
        self.assertNotIn("肉类及其制品", ingredients)
        self.assertEqual(
            ingredients[:6],
            ["金枪鱼", "酶解沙丁鱼", "酶解鳀鱼", "深海白鱼粉", "酶解三文鱼", "深海三文鱼油"],
        )
        self.assertIn("酶解鸡肉", ingredients)
        self.assertIn("菊苣根粉", ingredients)

    def test_ingredient_similarity_uses_order(self):
        exact = ingredient_similarity(["鸡肉", "鱼肉", "鸡油"], ["鸡肉", "鱼肉", "鸡油"])
        reordered = ingredient_similarity(["鸡肉", "鱼肉", "鸡油"], ["鱼肉", "鸡肉", "鸡油"])
        different = ingredient_similarity(["鸡肉", "鱼肉"], ["牛肉", "豌豆"])
        self.assertEqual(exact, 1.0)
        self.assertGreater(reordered, different)
        self.assertLess(reordered, exact)

    def test_ordered_similarity_weights_head_ingredients_more(self):
        exact = ordered_ingredient_similarity(
            ["鸡肉", "鸡肉粉", "鱼油", "南瓜"],
            ["鸡肉", "鸡肉粉", "鱼油", "南瓜"],
        )
        head_changed = ordered_ingredient_similarity(
            ["鸡肉", "鸡肉粉", "鱼油", "南瓜"],
            ["牛肉", "鸡肉粉", "鱼油", "南瓜"],
        )
        tail_changed = ordered_ingredient_similarity(
            ["鸡肉", "鸡肉粉", "鱼油", "南瓜"],
            ["鸡肉", "鸡肉粉", "鱼油", "胡萝卜"],
        )
        self.assertEqual(exact["score"], 1.0)
        self.assertLess(head_changed["score"], tail_changed["score"])
        self.assertLess(
            head_changed["weighted_position_similarity"],
            tail_changed["weighted_position_similarity"],
        )

    def test_nutrition_similarity_requires_matching_operator_and_unit(self):
        left = {
            "粗蛋白|>=|%": {
                "metric_name": "粗蛋白",
                "operator": ">=",
                "value": 38.0,
                "unit": "%",
            },
            "粗脂肪|>=|%": {
                "metric_name": "粗脂肪",
                "operator": ">=",
                "value": 18.0,
                "unit": "%",
            },
        }
        right = {
            **left,
            "粗纤维|<=|%": {
                "metric_name": "粗纤维",
                "operator": "<=",
                "value": 9.0,
                "unit": "%",
            },
        }
        evidence = nutrition_similarity(left, right)
        self.assertEqual(evidence["score"], 1.0)
        self.assertEqual(evidence["comparable_count"], 2)

    def test_merge_json_lists_preserves_unique_ocr_lineage(self):
        self.assertEqual(
            _merge_json_lists("[29]", [31, 29], None),
            [29, 31],
        )

    def test_preferred_candidate_status_keeps_reviewed_result(self):
        self.assertEqual(
            _preferred_value(
                "needs_manual_review",
                "approved",
                ("approved", "needs_manual_review", "pending", "rejected"),
            ),
            "approved",
        )

    def test_product_candidate_lineage_is_visible_for_manual_review(self):
        merged = _merge_product_candidate_lineage(
            None,
            source_id=459,
            parsed_row_id=569,
            raw_product_name="猎鸟乳鸽2.0",
            image_id="image-1",
        )
        self.assertEqual(merged["review_status"], "needs_manual_review")
        self.assertEqual(merged["source_ids_json"], "[459]")
        self.assertEqual(merged["parsed_row_ids_json"], "[569]")
        self.assertEqual(merged["raw_product_names_json"], '["猎鸟乳鸽2.0"]')

    def test_product_candidate_lineage_preserves_reviewed_status_and_merges_sources(self):
        merged = _merge_product_candidate_lineage(
            {
                "review_status": "approved",
                "source_ids_json": "[100]",
                "parsed_row_ids_json": "[200]",
                "raw_product_names_json": '["旧名称"]',
                "evidence_json": '{"reviewed": true}',
            },
            source_id=459,
            parsed_row_id=569,
            raw_product_name="猎鸟乳鸽2.0",
        )
        self.assertEqual(merged["review_status"], "approved")
        self.assertEqual(merged["source_ids_json"], "[100, 459]")
        self.assertIn('"reviewed": true', merged["evidence_json"])


if __name__ == "__main__":
    unittest.main()
