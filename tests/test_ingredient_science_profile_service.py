import unittest

from services.ingredient_science_profile_service import (
    CODE_COMPOSITE_RULES,
    empty_function_attributes,
    normalize_domain_attributes,
    inherited_domain_attributes,
    strip_identity_owned_attributes,
    normalize_function_attributes,
    suggest_science_profile,
    validate_science_profile,
)


def ingredient(name, role, family="", source_type="plant"):
    return {
        "standard_name": name,
        "primary_nutrition_role": role,
        "ingredient_family": family,
        "source_type": source_type,
    }


class IngredientScienceProfileTests(unittest.TestCase):
    def test_composite_rule_weights_sum_to_one(self):
        for score_code, _name, _group, components in CODE_COMPOSITE_RULES:
            self.assertAlmostEqual(sum(item[2] for item in components), 1.0, msg=score_code)

    def test_fiber_suggestion_is_conservative_draft(self):
        result = suggest_science_profile(ingredient("山核桃壳", "膳食纤维支持", "纤维来源类"))
        self.assertEqual(result["nutrition_category"], "fiber")
        self.assertEqual(result["nutrition_subtype"], "other")
        self.assertEqual(result["science_status"], "draft")
        self.assertEqual(set(result["function_attributes"].values()), {"unknown"})

    def test_hydrolyzed_protein_suggestion(self):
        result = suggest_science_profile(ingredient("酶解牛肉粉", "蛋白质供给", "牛肉类", "animal"))
        self.assertEqual(result["nutrition_category"], "protein")
        self.assertEqual(result["nutrition_subtype"], "hydrolyzed")
        self.assertEqual(result["domain_attributes"]["protein_form"], "unknown")

    def test_domain_attributes_use_category_specific_enums(self):
        result = normalize_domain_attributes(
            "fiber",
            {
                "fiber_solubility": "mixed",
                "fermentability": "medium",
                "fiber_functions": ["bulk", "buffer"],
                "prebiotic_functions": ["scfa_support"],
            },
        )
        self.assertEqual(result["fiber_solubility"], "mixed")
        self.assertEqual(result["fiber_functions"], ["bulk", "buffer"])

    def test_domain_attributes_reject_cross_domain_fields(self):
        with self.assertRaisesRegex(ValueError, "未知领域属性"):
            normalize_domain_attributes("carbohydrate", {"protein_form": "fresh"})

    def test_plant_protein_form_uses_dedicated_enum(self):
        result = normalize_domain_attributes(
            "protein",
            {"plant_protein_form": "meal", "protein_form": "none", "animal_source_category": "none"},
        )
        self.assertEqual(result["plant_protein_form"], "meal")
        with self.assertRaisesRegex(ValueError, "枚举值无效"):
            normalize_domain_attributes("protein", {"plant_protein_form": "mild"})

    def test_compact_carb_and_fiber_enums(self):
        carb = normalize_domain_attributes(
            "carbohydrate", {"starch_category": "available_sugar"}
        )
        self.assertEqual(carb["starch_category"], "available_sugar")
        fiber = normalize_domain_attributes(
            "fiber", {"fiber_functions": ["forming", "gel_forming"]}
        )
        self.assertEqual(fiber["fiber_functions"], ["forming", "gel_forming"])

    def test_antioxidant_and_mineral_domain_enums(self):
        antioxidant = normalize_domain_attributes(
            "antioxidant",
            {
                "antioxidant_type": "plant_extract",
                "antioxidant_functions": ["lipid_protection", "radical_scavenging"],
            },
        )
        self.assertEqual(antioxidant["antioxidant_type"], "plant_extract")
        mineral = normalize_domain_attributes(
            "mineral",
            {"mineral_type": "chelated", "mineral_elements": ["zinc", "copper"]},
        )
        self.assertEqual(mineral["mineral_elements"], ["zinc", "copper"])

    def test_identity_fields_are_inherited_not_stored_twice(self):
        inherited = inherited_domain_attributes(
            "protein", {"source_type": "animal", "animal_source": "鸡"}
        )
        self.assertEqual(inherited, {"protein_source": "animal", "animal_source": "chicken"})
        stored = strip_identity_owned_attributes(
            "protein",
            {"protein_source": "plant", "animal_source": "fish", "protein_form": "fresh"},
        )
        self.assertEqual(stored, {"protein_form": "fresh"})

    def test_function_attributes_reject_unknown_keys_and_strengths(self):
        with self.assertRaisesRegex(ValueError, "未知功能属性"):
            normalize_function_attributes({"marketing_claim": "strong"})
        with self.assertRaisesRegex(ValueError, "强度无效"):
            normalize_function_attributes({"bulk_support": "very_strong"})

    def test_partial_function_attributes_fill_unknown_values(self):
        result = normalize_function_attributes({"bulk_support": "strong"})
        expected = empty_function_attributes()
        expected["bulk_support"] = "strong"
        self.assertEqual(result, expected)

    def test_subtype_must_match_category(self):
        with self.assertRaisesRegex(ValueError, "不匹配"):
            validate_science_profile(
                {
                    "nutrition_category": "fiber",
                    "nutrition_subtype": "fresh",
                    "function_attributes": {},
                    "science_status": "draft",
                    "evidence_level": "unknown",
                }
            )


if __name__ == "__main__":
    unittest.main()
