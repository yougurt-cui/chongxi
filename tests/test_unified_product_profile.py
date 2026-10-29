import unittest
from unittest.mock import MagicMock

from services.unified_product_profile_service import (
    build_assessment,
    build_brand_performance,
    build_structure_profile,
    ensure_table,
)


class UnifiedProductProfileTest(unittest.TestCase):
    def test_structure_profile_prioritizes_core_formula_features(self):
        result = build_structure_profile([
            {"level1": "适口性结构", "level2": "表面诱食结构"},
            {"level1": "蛋白结构", "level2": "多肉源结构"},
            {"level1": "碳水/淀粉结构", "level2": "豆类淀粉结构"},
            {"level1": "肠道功能结构", "level2": "益生元+纤维复合结构"},
        ])
        self.assertEqual(result["primary_type"], "多肉源+豆类淀粉型")
        self.assertEqual(result["core_features"][0]["label"], "多肉源结构")
        self.assertNotIn("表面诱食结构", result["headline"])

    def test_assessment_keeps_absolute_and_market_meanings_separate(self):
        portrait = {"dimensions": {
            "prebiotic": {"score": 1.94, "scale_max": 5, "level": "medium_low",
                           "label": "益生元支持中低", "direction": "higher_is_support"},
            "protein_quality": {"score": .8, "scale_max": 1, "level": "high",
                                "label": "蛋白质量较高", "direction": "higher_is_support"},
            "carbohydrate": {"score": 4.38, "scale_max": 5, "level": "high",
                             "label": "碳水负担较高", "direction": "higher_is_burden"},
        }}
        market = {"rankings": {"益生元": 94.0, "淀粉碳水": 2.9, "动物蛋白": 5},
                  "advantages": ["益生元"], "weaknesses": ["淀粉碳水", "动物蛋白"]}
        strengths, cautions, _ = build_assessment(portrait, market)
        self.assertIn("绝对水平为益生元支持中低", strengths[0]["summary"])
        self.assertIn("市场中差异化不强", strengths[1]["summary"])
        self.assertIn("市场中也处于相对弱项", cautions[0]["summary"])

    def test_brand_performance_aggregates_products(self):
        rows = [
            {"brand": "A", "structure_profile": {"primary_type": "类型1"},
             "market_performance": {"rankings": {"纤维": 100, "脂肪": 20}}},
            {"brand": "A", "structure_profile": {"primary_type": "类型1"},
             "market_performance": {"rankings": {"纤维": 100, "脂肪": 30}}},
            {"brand": "B", "structure_profile": {"primary_type": "类型2"},
             "market_performance": {"rankings": {"纤维": 10, "脂肪": 90}}},
        ]
        result = build_brand_performance(rows)
        self.assertEqual(result["A"]["product_count"], 2)
        self.assertEqual(result["A"]["average_rankings"]["纤维"], 100)
        self.assertIn("纤维", result["A"]["advantages"])

    def test_schema_contains_all_profile_sections(self):
        conn = MagicMock()
        ensure_table(conn)
        sql = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
        for column in ("structure_profile_json", "six_dimension_json", "market_performance_json",
                       "brand_performance_json", "strengths_json", "cautions_json"):
            self.assertIn(column, sql)
        conn.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
