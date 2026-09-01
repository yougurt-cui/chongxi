import unittest

from services.fiber_science_materialization_service import build_science_payload, structure_labels


class FiberScienceMaterializationTest(unittest.TestCase):
    def test_builds_compatible_payload_without_name_rules(self):
        items = [
            {"position": 2, "raw_name": "任意名称甲", "standard_name": "标准豆", "standard_ingredient_id": "A", "primary_nutrition_role": "碳水供给", "is_ignored": 0},
            {"position": 5, "raw_name": "任意名称乙", "standard_name": "标准胶质纤维", "standard_ingredient_id": "B", "primary_nutrition_role": "膳食纤维支持", "is_ignored": 0},
        ]
        profiles = {
            "A": {"science_status": "active", "nutrition_category": "carbohydrate", "profile_version": 2, "domain_attributes_json": {"starch_category": "legume"}},
            "B": {"science_status": "active", "nutrition_category": "fiber", "profile_version": 3, "domain_attributes_json": {"fiber_solubility": "soluble", "fermentability": "medium", "fiber_functions": ["forming", "gel_forming"], "prebiotic_functions": []}},
        }
        result = build_science_payload(items, profiles)
        self.assertEqual(result["science_profile_coverage"], 1.0)
        self.assertEqual(result["starch_ingredients_json"][0]["category"], "豆类碳水来源")
        self.assertEqual(result["ingredient_feature_json"]["ingredient_tag_detail"]["标准胶质纤维"]["fiber_functions"], ["吸水成形", "胶质成形"])
        labels = structure_labels(result["ingredient_feature_json"], result["starch_ingredients_json"])
        self.assertEqual(labels["starch"], ["豆类淀粉结构"])
        self.assertEqual(labels["fiber"], ["胶质纤维结构"])

    def test_missing_active_profile_marks_review(self):
        result = build_science_payload(
            [{"position": 1, "standard_ingredient_id": "A", "standard_name": "待审核碳水", "primary_nutrition_role": "碳水供给"}],
            {"A": {"science_status": "draft", "nutrition_category": "carbohydrate"}},
        )
        self.assertEqual(result["profile_status"], "needs_review")
        self.assertEqual(result["science_profile_coverage"], 0.0)
        self.assertEqual(len(result["missing_science_profiles"]), 1)


if __name__ == "__main__":
    unittest.main()

