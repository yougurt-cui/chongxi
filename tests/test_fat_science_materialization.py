import unittest

from services.fat_science_materialization_service import build_science_features


class FatScienceMaterializationTest(unittest.TestCase):
    def test_uses_only_fat_profiles_for_omega_labels(self):
        items = [
            {"position": 1, "standard_ingredient_id": "FISH", "standard_name": "鳕鱼", "primary_nutrition_role": "蛋白质供给"},
            {"position": 2, "standard_ingredient_id": "OIL", "standard_name": "鱼油", "primary_nutrition_role": "脂肪酸支持"},
        ]
        profiles = {
            "FISH": {"science_status": "active", "nutrition_category": "protein", "profile_version": 1, "domain_attributes_json": {"animal_source_category": "fish"}},
            "OIL": {"science_status": "active", "nutrition_category": "fat", "profile_version": 2, "domain_attributes_json": {"fat_source": "marine", "fat_functions": ["energy", "omega3"]}},
        }
        result = build_science_features(items, profiles)
        self.assertEqual(result["fat_sources"], "鱼油")
        self.assertEqual(result["omega3_sources"], "鱼油")
        self.assertNotIn("鳕鱼", result["omega3_sources"])

    def test_adds_secondary_micronutrient_without_changing_protein_category(self):
        items = [{"standard_ingredient_id": "LIVER", "standard_name": "鸡肝", "primary_nutrition_role": "蛋白质供给"}]
        profiles = {
            "LIVER": {
                "science_status": "active", "nutrition_category": "protein", "profile_version": 3,
                "domain_attributes_json": {"micronutrient_source_type": "animal_organ"},
                "function_attributes_json": {"micronutrient_support": "strong"},
            }
        }
        result = build_science_features(items, profiles)
        self.assertEqual(result["micronutrient_sources"], "动物内脏")
        self.assertEqual(result["micronutrient_types"], "动物内脏")
        self.assertIsNone(result["omega3_sources"])


if __name__ == "__main__":
    unittest.main()
