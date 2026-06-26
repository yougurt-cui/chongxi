import unittest

from scripts.build_catfood_standard_products import (
    choose_display_name,
    extract_flavor_or_protein,
    extract_model,
    extract_process,
    hard_filter_candidate,
    validate_model_result,
)


class StandardProductExtractionTest(unittest.TestCase):
    def test_model_excludes_weight_and_nutrition_units(self):
        text = "产品名称 全价猫粮 规格 2KG 粗蛋白 40% 维生素B12 5MG"
        self.assertEqual(extract_model(text, "全价猫粮"), "")

    def test_model_accepts_repeated_packaging_code(self):
        text = "全龄段全猫 R9 R9 湿粮 原料组成 鲜鸡肉62%"
        self.assertEqual(extract_model(text, "全龄段全猫"), "R9")

    def test_process_does_not_read_ingredient_section(self):
        text = "原始猎食猫粮 原料组成 冻干鸡肉4%、鲜鸡肉30%"
        self.assertEqual(extract_process(text, "原始猎食猫粮"), "")

    def test_flavor_ignores_natural_flavor_enhancer(self):
        text = "全价猫粮 原料组成 天然口味增强剂、鸡肉粉"
        self.assertEqual(extract_flavor_or_protein(text, "全价猫粮"), "")

    def test_display_name_priority(self):
        value = choose_display_name(
            model_name="P40",
            series_name="鲜肉系列",
            function_name="低敏",
            process_name="烘焙",
            flavor_or_protein="鸡肉",
            life_stage="成猫",
            standard_product_name="全价猫粮",
            brand_name="测试",
        )
        self.assertEqual(value, ("P40", "model", "strong"))

    def test_flavor_display_uses_adult_suffix(self):
        value = choose_display_name(
            model_name="",
            series_name="",
            function_name="",
            process_name="",
            flavor_or_protein="三文鱼",
            life_stage="成猫",
            standard_product_name="三文鱼配方",
            brand_name="测试",
        )
        self.assertEqual(value, ("三文鱼成猫粮", "flavor_or_protein", "medium"))

    def test_hard_filter_rejects_additive_sentence(self):
        passed, reason = hard_filter_candidate(
            "维生素A乙酸酯、维生素D3、硝酸硫胺、核黄素、盐酸吡哆醇"
        )
        self.assertFalse(passed)
        self.assertEqual(reason, "nutrition_or_additive_sentence")

    def test_hard_filter_rejects_formula_heading(self):
        self.assertEqual(
            hard_filter_candidate("配方成分分析"),
            (False, "formula_analysis_heading"),
        )

    def test_model_validation_strips_brand(self):
        result = validate_model_result(
            {
                "is_product_name_candidate": True,
                "product_name_display": "麦富迪 N5",
                "product_name_subtitle": None,
                "candidate_type": "model_code",
                "name_quality": "strong",
                "review_status": "pending",
                "normalized_tags": [],
                "truncation_suspected": False,
                "reject_reason": None,
                "reason": "明确型号",
            },
            raw_text="麦富迪 N5",
            brand_name="麦富迪",
            aliases=[],
        )
        self.assertEqual(result["product_name_display"], "N5")
        self.assertEqual(result["name_quality"], "strong")

    def test_generic_function_is_not_main_display(self):
        result = validate_model_result(
            {
                "is_product_name_candidate": True,
                "product_name_display": "低敏",
                "product_name_subtitle": None,
                "candidate_type": "function_position",
                "name_quality": "medium",
                "review_status": "pending",
                "normalized_tags": [],
                "truncation_suspected": False,
                "reject_reason": None,
                "reason": "功能词",
            },
            raw_text="低敏",
            brand_name="",
            aliases=[],
        )
        self.assertIsNone(result["product_name_display"])
        self.assertEqual(result["product_name_subtitle"], "低敏")
        self.assertEqual(result["name_quality"], "weak")

    def test_truncated_name_is_pending(self):
        result = validate_model_result(
            {
                "is_product_name_candidate": True,
                "product_name_display": "野性本能中大型全",
                "candidate_type": "official_name",
                "name_quality": "strong",
                "review_status": "needs_manual_review",
            },
            raw_text="野性本能中大型全",
            brand_name="",
            aliases=[],
        )
        self.assertTrue(result["truncation_suspected"])
        self.assertEqual(result["review_status"], "pending")
        self.assertEqual(result["name_quality"], "medium")

    def test_unknown_type_is_recovered_for_official_name(self):
        result = validate_model_result(
            {
                "is_product_name_candidate": True,
                "product_name_display": "原始猎食原味",
                "candidate_type": "official_product_name",
                "name_quality": "high",
                "review_status": "needs_review",
            },
            raw_text="商品名称：原始猎食原味猫粮",
            brand_name="渴望",
            aliases=["Orijen"],
        )
        self.assertEqual(result["candidate_type"], "official_name")
        self.assertEqual(result["name_quality"], "strong")
        self.assertEqual(result["review_status"], "needs_manual_review")

    def test_life_stage_can_be_main_display(self):
        result = validate_model_result(
            {
                "is_product_name_candidate": True,
                "product_name_display": "幼猫",
                "candidate_type": "life_stage",
                "name_quality": "medium",
                "review_status": "pending",
            },
            raw_text="全价幼年期猫粮",
            brand_name="",
            aliases=[],
        )
        self.assertEqual(result["product_name_display"], "幼猫粮")
        self.assertEqual(result["candidate_type"], "life_stage")
        self.assertEqual(result["name_quality"], "medium")
        self.assertEqual(result["review_status"], "needs_manual_review")

    def test_model_official_type_is_corrected_for_life_stage(self):
        result = validate_model_result(
            {
                "is_product_name_candidate": True,
                "product_name_display": "幼年猫猫粮",
                "candidate_type": "official_name",
                "name_quality": "strong",
                "review_status": "needs_manual_review",
            },
            raw_text="全价幼年期猫粮",
            brand_name="",
            aliases=[],
        )
        self.assertEqual(result["product_name_display"], "幼猫粮")
        self.assertEqual(result["candidate_type"], "life_stage")
        self.assertEqual(result["name_quality"], "medium")


if __name__ == "__main__":
    unittest.main()
