import sys
import unittest
from pathlib import Path

import pandas as pd


APP_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = APP_DIR / "vendor" / "feature_score_pipeline" / "scripts"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import protein_score1  # noqa: E402
import rebuild_protein_source_aggregate as protein_aggregate  # noqa: E402
from scripts import backfill_formula_ingredient_features as feature_backfill  # noqa: E402


def standard_lookup():
    rows = [
        {
            "standard_ingredient_id": "STD00002",
            "standard_name": "鲜鸡肉",
            "ingredient_family": "鸡肉类",
            "source_type": "animal",
            "animal_source": "鸡",
            "primary_nutrition_role": "动物蛋白",
            "confidence": 1.0,
        },
        {
            "standard_ingredient_id": "STD00075",
            "standard_name": "冻鸡肉",
            "ingredient_family": "鸡肉类",
            "source_type": "animal",
            "animal_source": "鸡",
            "primary_nutrition_role": "动物蛋白",
            "confidence": 1.0,
        },
        {
            "standard_ingredient_id": "STD00003",
            "standard_name": "鸡肉粉",
            "ingredient_family": "鸡肉类",
            "source_type": "animal",
            "animal_source": "鸡",
            "primary_nutrition_role": "动物蛋白",
            "confidence": 1.0,
        },
        {
            "standard_ingredient_id": "STD90001",
            "standard_name": "豌豆蛋白",
            "ingredient_family": "豆类/植物蛋白类",
            "source_type": "plant",
            "animal_source": "",
            "primary_nutrition_role": "植物蛋白",
            "confidence": 1.0,
        },
    ]
    return {
        protein_aggregate._normalize_ingredient_key(row["standard_name"]): row
        for row in rows
    }


def protein_rules():
    return [
        {
            "rule_id": 101,
            "match_scope": "primary_nutrition_role",
            "match_value": "植物蛋白",
            "feature_domain": "protein",
            "feature_key": "is_protein",
            "feature_value": "true",
            "priority": 310,
        },
        {
            "rule_id": 102,
            "match_scope": "primary_nutrition_role",
            "match_value": "植物蛋白",
            "feature_domain": "protein",
            "feature_key": "is_plant_protein",
            "feature_value": "true",
            "priority": 310,
        },
        {
            "rule_id": 103,
            "match_scope": "raw_name",
            "match_value": "水解",
            "feature_domain": "protein",
            "feature_key": "form",
            "feature_value": "水解蛋白",
            "priority": 110,
        },
        {
            "rule_id": 104,
            "match_scope": "raw_name",
            "match_value": "鲜",
            "feature_domain": "protein",
            "feature_key": "form",
            "feature_value": "鲜肉",
            "priority": 90,
        },
        {
            "rule_id": 105,
            "match_scope": "raw_name",
            "match_value": "冻",
            "feature_domain": "protein",
            "feature_key": "form",
            "feature_value": "冻肉",
            "priority": 90,
        },
        {
            "rule_id": 106,
            "match_scope": "raw_name",
            "match_value": "冻干",
            "feature_domain": "protein",
            "feature_key": "form",
            "feature_value": "鲜肉",
            "priority": 95,
        },
    ]


class ProteinStandardizationPipelineTest(unittest.TestCase):
    def test_carb_role_is_not_promoted_by_mixed_family_name(self):
        item = {
            "raw_name": "鹰嘴豆",
            "standard_name": "鹰嘴豆",
            "ingredient_family": "豆类/植物蛋白类",
            "source_type": "plant",
            "primary_nutrition_role": "碳水供给",
        }
        self.assertFalse(protein_aggregate._is_standard_protein_item(item))
        self.assertFalse(protein_aggregate._is_plant_protein_item(item))

    def test_explicit_plant_protein_overrides_carb_role_guard(self):
        item = {
            "raw_name": "豌豆蛋白",
            "standard_name": "豌豆蛋白",
            "ingredient_family": "豆类/植物蛋白类",
            "source_type": "plant",
            "primary_nutrition_role": "碳水供给",
        }
        self.assertTrue(protein_aggregate._is_standard_protein_item(item))
        self.assertTrue(protein_aggregate._is_plant_protein_item(item))

    def test_candidate_name_normalization_removes_brackets_and_converts_script(self):
        cases = {
            "魚油（三文魚油）": "鱼油",
            "雞肉[去骨]": "鸡肉",
            "沙丁魚（、影魚": "沙丁鱼",
            "雞肉【新鮮】粉": "鸡肉粉",
            "雞肉】": "鸡肉",
            "９０": "90",
            "硝酸硫胺8mgkg": "硝酸硫胺",
            "葡萄糖胺1000mg/kg": "葡萄糖胺",
            "牛磺酸0.2%": "牛磺酸",
            "β-胡萝卜素3741kcal/公斤": "β胡萝卜素",
            "嗜酸乳杆菌1.0×1010CFU/kg": "嗜酸乳杆菌",
            "维生素B12": "维生素b12",
        }
        for raw_name, expected in cases.items():
            with self.subTest(raw_name=raw_name):
                self.assertEqual(
                    protein_aggregate._normalize_ingredient_key(raw_name),
                    expected,
                )
        self.assertTrue(protein_aggregate._normalize_ingredient_key("９０").isdigit())
        self.assertEqual(
            protein_aggregate._ingredient_candidate_noise_reason("dha"),
            "pure_latin_letters",
        )
        self.assertEqual(
            protein_aggregate._ingredient_candidate_noise_reason("钙"),
            "single_han_character",
        )
        self.assertIsNone(
            protein_aggregate._ingredient_candidate_noise_reason("维生素b12")
        )

    def test_additives_are_excluded_before_protein_standardization(self):
        rows = protein_aggregate.transform_rows(
            [
                {
                    "source_id": 1,
                    "formula_id": 1001,
                    "brand": "测试",
                    "product_name": "复杂粮",
                    "ingredient_composition": (
                        "鲜鸡肉60%、冻鸡肉10%、鸡肉粉8%、豌豆蛋白2%、"
                        "添加剂组成：牛磺酸、维生素A、硫酸锌"
                    ),
                    "guarantee_metric_name": "粗蛋白",
                    "guarantee_metric_value": 42,
                    "guarantee_metric_unit": "%",
                }
            ],
            standard_lookup=standard_lookup(),
        )
        row = rows[0]
        self.assertEqual(row["protein_source_details"], "鲜鸡肉、冻鸡肉、鸡肉粉、豌豆蛋白")
        self.assertNotIn("牛磺酸", row["protein_source_details"])
        self.assertNotIn("维生素", row["protein_source_details"])
        self.assertEqual(row["primary_meat_source_type"], "鲜肉")
        self.assertEqual(row["secondary_meat_source_type"], "冻肉、肉粉")
        self.assertEqual(row["meat_source_complexity"], "单一来源")
        self.assertEqual(row["plant_protein_labels"], "豌豆蛋白")

    def test_grouped_ingredient_headers_expand_before_standardization(self):
        text = (
            "鱼类等水生生物及其制品43%(金枪鱼、酶解沙丁鱼、酶解鳀鱼、深海白鱼粉、酶解三文鱼、深海三文鱼油)、"
            "肉类及其制品42%、酶解鸡肉、鸡肉粉、鸡肉、鸡油、牛油、酶解鸡肝)、"
            "果蔬类籽实及其制品(紫薯、马铃薯、南瓜、苹果、梨、蔓越莓、菊苣根粉)"
        )
        tokens = protein_aggregate._split_ingredient_tokens(text)
        self.assertNotIn("鱼类等水生生物及其制品", tokens)
        self.assertNotIn("肉类及其制品", tokens)
        self.assertEqual(
            tokens[:6],
            ["金枪鱼", "酶解沙丁鱼", "酶解鳀鱼", "深海白鱼粉", "酶解三文鱼", "深海三文鱼油"],
        )
        items = protein_aggregate._standardize_ingredient_items(text, feature_rules=protein_rules())
        self.assertEqual(items[0]["raw_name"], "金枪鱼")
        self.assertEqual(items[1]["protein_form"], "水解蛋白")
        fish_oil = next(item for item in items if item["raw_name"] == "深海三文鱼油")
        self.assertFalse(fish_oil["is_protein"])

    def test_protein_score_regression_for_standardized_formula(self):
        rows = protein_aggregate.transform_rows(
            [
                {
                    "source_id": 1,
                    "formula_id": 1001,
                    "brand": "测试",
                    "product_name": "复杂粮",
                    "ingredient_composition": (
                        "鲜鸡肉60%、冻鸡肉10%、鸡肉粉8%、豌豆蛋白2%、"
                        "添加剂组成：牛磺酸、维生素A"
                    ),
                    "guarantee_metric_name": "粗蛋白",
                    "guarantee_metric_value": 42,
                    "guarantee_metric_unit": "%",
                },
                {
                    "source_id": 2,
                    "formula_id": 1002,
                    "brand": "测试",
                    "product_name": "简单粮",
                    "ingredient_composition": "鲜鸡肉80%、米、添加剂组成：牛磺酸",
                    "guarantee_metric_name": "粗蛋白",
                    "guarantee_metric_value": 30,
                    "guarantee_metric_unit": "%",
                },
            ],
            standard_lookup=standard_lookup(),
        )
        scored = protein_score1.add_score_columns(pd.DataFrame(rows))
        target = scored[scored["source_id"] == 1].iloc[0].to_dict()

        self.assertEqual(target["meat_source_complexity_score"], 1.0)
        self.assertEqual(target["main_protein_form_score"], 1.0)
        self.assertEqual(target["secondary_protein_form_score"], 1.0)
        self.assertEqual(target["plant_protein_interference_norm"], "2级｜单一高浓缩型植物蛋白")
        self.assertEqual(target["plant_protein_interference_score"], 0.4)
        self.assertEqual(target["protein_structure_score"], 0.664)
        self.assertEqual(target["protein_quality_score"], 0.919)

    def test_animal_dominance_does_not_double_count_plant_interference(self):
        base = {
            "animal_source_level1_categories": "禽类",
            "animal_source_level2_sources": "鸡",
            "animal_sources": "鸡",
        }
        without_interference = protein_score1.calc_animal_protein_dominance_score(
            {**base, "plant_protein_interference_score": 0.0}
        )
        with_interference = protein_score1.calc_animal_protein_dominance_score(
            {**base, "plant_protein_interference_score": 1.0}
        )
        self.assertEqual(with_interference, without_interference)

    def test_animal_dominance_prefers_position_contribution_ratio(self):
        score = protein_score1.calc_animal_protein_dominance_score(
            {
                "animal_protein_dominance_score": 0.72,
                "animal_source_level1_categories": "禽类",
                "animal_source_level2_sources": "鸡",
            }
        )
        self.assertEqual(score, 0.72)

    def test_repeated_same_animal_source_is_single_source_complexity(self):
        items = [
            {"animal_source": "鸡", "raw_name": "鲜鸡肉"},
            {"animal_source": "鸡", "raw_name": "鸡肉粉"},
            {"animal_source": "鸡", "raw_name": "鸡肝粉"},
        ]
        self.assertEqual(protein_aggregate._infer_meat_source_complexity(items), "单一来源")

    def test_main_protein_form_aggregates_declared_ratios_across_ingredients(self):
        rows = protein_aggregate.transform_rows(
            [{
                "source_id": 9, "formula_id": 9009,
                "ingredient_composition": "鲜鸡肉35%、冻鸭肉20%、鸡肉粉25%、木薯粉20%",
            }],
            standard_lookup=standard_lookup(),
        )
        row = rows[0]
        self.assertEqual(row["primary_meat_source_type"], "鲜肉/肉粉/冻肉")
        self.assertIsNone(row["secondary_meat_source_type"])
        self.assertAlmostEqual(row["protein_form_contribution_shares"]["鲜肉"], 0.4375)
        self.assertAlmostEqual(
            protein_score1.calc_main_protein_form_score(row),
            1.4375,
        )

    def test_item_feature_tags_include_fat_fiber_and_starch_domains(self):
        item_cases = {
            "鱼油": {
                "fat.fat_sources": "鱼油",
                "fat.omega3_sources": "鱼油",
                "fat.fat_source_types": "动物脂肪",
            },
            "甘薯粉": {
                "starch.category": "薯类淀粉来源",
                "starch.base_score": 1.5,
            },
            "胡萝卜粉": {
                "fiber.standard_tag": "胡萝卜粉",
                "fiber.category": "膳食纤维",
                "fiber.fermentability": "中",
            },
        }
        for raw_name, expected_features in item_cases.items():
            with self.subTest(raw_name=raw_name):
                features = feature_backfill._features_for_item(
                    {"raw_name": raw_name, "is_protein": False}
                )
                for key, expected_value in expected_features.items():
                    self.assertEqual(features[key], expected_value)

    def test_protein_rules_override_fallback_by_priority(self):
        items = protein_aggregate._standardize_ingredient_items(
            "鲜水解鸡肉、冻干鸡肉",
            feature_rules=protein_rules(),
        )
        self.assertEqual(items[0]["protein_form"], "水解蛋白")
        self.assertEqual(items[1]["protein_form"], "鲜肉")
        self.assertEqual(items[0]["protein_rule_ids"], [103])
        self.assertEqual(items[1]["protein_rule_ids"], [106])

    def test_active_science_profile_overrides_name_form_fallback(self):
        standard = {
            "standard_ingredient_id": "STD99001",
            "standard_name": "测试蛋白原料",
            "ingredient_family": "鸡肉类",
            "source_type": "animal",
            "animal_source": "鸡",
            "primary_nutrition_role": "蛋白质供给",
            "science_status": "active",
            "science_nutrition_category": "protein",
            "science_profile_version": 3,
            "science_attributes": {"protein_form": "hydrolyzed", "source_specificity": "specific"},
            "confidence": 1.0,
        }
        lookup = {protein_aggregate._normalize_ingredient_key(standard["standard_name"]): standard}
        item = protein_aggregate._standardize_ingredient_items(standard["standard_name"], lookup)[0]
        self.assertEqual(item["protein_form"], "水解蛋白")
        self.assertEqual(item["protein_form_origin"], "science_profile")
        self.assertEqual(item["science_profile_version"], 3)

    def test_active_non_protein_science_profile_overrides_legacy_name_rule(self):
        standard = {
            "standard_ingredient_id": "STD-MINERAL", "standard_name": "蛋白锌",
            "source_type": "other", "primary_nutrition_role": "矿物质补充",
            "science_status": "active", "science_nutrition_category": "mineral",
            "science_attributes": {}, "confidence": 1.0,
        }
        lookup = {protein_aggregate._normalize_ingredient_key(standard["standard_name"]): standard}
        item = protein_aggregate._standardize_ingredient_items("蛋白锌", lookup)[0]
        self.assertFalse(item["is_protein"])

    def test_active_plant_protein_counts_as_science_coverage_without_protein_form(self):
        standard = {
            "standard_ingredient_id": "STD-PLANT", "standard_name": "大豆粕",
            "source_type": "plant", "primary_nutrition_role": "蛋白质供给",
            "science_status": "active", "science_nutrition_category": "protein",
            "science_attributes": {"protein_form": "none", "plant_protein_form": "meal"},
            "confidence": 1.0,
        }
        lookup = {protein_aggregate._normalize_ingredient_key(standard["standard_name"]): standard}
        labels = protein_aggregate._protein_labels_from_standard_items("大豆粕", lookup)
        self.assertEqual(labels["science_profile_coverage"], 1.0)
        self.assertEqual(labels["science_profile_missing"], [])

    def test_structured_role_rule_recovers_plant_protein(self):
        lookup = standard_lookup()
        custom = {
            "standard_ingredient_id": "STD90002",
            "standard_name": "豌豆粉浆蛋白粉",
            "ingredient_family": "豆类/植物蛋白类",
            "source_type": "plant",
            "animal_source": "",
            "primary_nutrition_role": "植物蛋白",
            "confidence": 1.0,
        }
        lookup[protein_aggregate._normalize_ingredient_key(custom["standard_name"])] = custom
        item = protein_aggregate._standardize_ingredient_items(
            custom["standard_name"],
            lookup,
            protein_rules(),
        )[0]
        self.assertTrue(item["is_protein"])
        self.assertTrue(item["is_plant_protein"])
        self.assertEqual(item["protein_rule_ids"], [101, 102])

    def test_uniquely_splits_concatenated_standard_ingredients(self):
        lookup = standard_lookup()
        for row in (
            {
                "standard_ingredient_id": "STD91001", "standard_name": "小麦蛋白",
                "ingredient_family": "谷物/植物蛋白类", "source_type": "plant",
                "animal_source": "", "primary_nutrition_role": "植物蛋白", "confidence": 1.0,
            },
            {
                "standard_ingredient_id": "STD91002", "standard_name": "甜菜粕",
                "ingredient_family": "膳食纤维类", "source_type": "plant",
                "animal_source": "", "primary_nutrition_role": "膳食纤维", "confidence": 1.0,
            },
        ):
            lookup[protein_aggregate._normalize_ingredient_key(row["standard_name"])] = row

        items = protein_aggregate._standardize_ingredient_items(
            "**小麦蛋白甜菜粕",
            lookup,
            protein_rules(),
        )

        self.assertEqual([item["standard_name"] for item in items], ["小麦蛋白", "甜菜粕"])
        self.assertEqual([item["position"] for item in items], [1, 2])
        self.assertTrue(all(item["match_method"] == "standard_alias_compound_split" for item in items))

    def test_does_not_split_an_existing_complete_ingredient(self):
        lookup = standard_lookup()
        items = protein_aggregate._standardize_ingredient_items("鲜鸡肉", lookup, protein_rules())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["standard_name"], "鲜鸡肉")
        self.assertEqual(items[0]["match_method"], "standard_alias")

    def test_rule_trace_is_written_to_item_features(self):
        features = feature_backfill._features_for_item(
            {
                "raw_name": "鲜水解鸡肉",
                "is_protein": True,
                "is_plant_protein": False,
                "protein_form": "水解蛋白",
                "protein_rule_features": {"protein.form": "水解蛋白"},
                "protein_rule_ids": [103],
            }
        )
        self.assertEqual(features["protein.form"], "水解蛋白")
        self.assertEqual(features["protein.rule_ids"], [103])


if __name__ == "__main__":
    unittest.main()
