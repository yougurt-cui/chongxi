import unittest

from services import miniprogram_food_change_service as service


class MiniProgramFoodChangeServiceTest(unittest.TestCase):
    def test_parse_fenced_json(self):
        result = service._parse_json_object('```json\n{"is_food_change_intent": true}\n```')
        self.assertIs(result["is_food_change_intent"], True)

    def test_normalize_cat_status_drops_unknown_fields(self):
        result = service._normalize_cat_status({"age": "3岁", "symptoms": ["软便"], "secret": "x"})
        self.assertEqual(result, {"age": "3岁", "symptoms": ["软便"]})

    def test_candidate_score_prefers_matching_brand_and_product(self):
        exact = service._candidate_score("皇家", "肠胃舒适成猫粮", {"standard_brand": "皇家", "product_name": "肠胃舒适成猫粮"})
        other = service._candidate_score("皇家", "肠胃舒适成猫粮", {"standard_brand": "渴望", "product_name": "六种鱼"})
        self.assertGreater(exact, 0.9)
        self.assertGreater(exact, other)

    def test_brand_only_does_not_guess_a_product(self):
        self.assertIsNone(service.match_catalog_product("皇家", ""))

    def test_product_list_requires_brand(self):
        with self.assertRaisesRegex(ValueError, "brand 不能为空"):
            service.list_catalog_products_by_brand("")

    def test_analyze_rejects_empty_message(self):
        with self.assertRaisesRegex(ValueError, "message 不能为空"):
            service.analyze_and_store({"message": ""})

    def test_ingredient_categories_use_database_semantics(self):
        self.assertEqual(service._ingredient_category({"source_type": "animal", "is_protein": 1}), "animal_protein")
        self.assertEqual(service._ingredient_category({"is_plant_protein": 1}), "plant_protein")
        self.assertEqual(service._ingredient_category({"primary_nutrition_role": "脂肪酸支持"}), "fat")
        self.assertEqual(service._ingredient_category({"ingredient_family": "纤维来源类"}), "fiber")


if __name__ == "__main__":
    unittest.main()
