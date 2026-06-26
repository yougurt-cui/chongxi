import unittest

from scripts.initialize_catfood_standard_formulas import (
    FormulaSource,
    cluster_formula_sources,
    should_merge_formula_sources,
)


def source(
    source_id,
    ingredients,
    fingerprint,
    nutrition=None,
):
    return FormulaSource(
        source_id=source_id,
        parsed_row_id=source_id,
        file_sha256="",
        raw_brand_name="测试",
        raw_product_name="测试产品",
        raw_ingredient_composition="、".join(ingredients),
        normalized_composition="，".join(ingredients),
        ingredients=ingredients,
        fingerprint=fingerprint,
        nutrition=nutrition or {},
    )


class InitializeStandardFormulasTest(unittest.TestCase):
    def test_exact_ordered_fingerprint_merges_without_nutrition(self):
        left = source(1, ["鸡肉", "鸡肉粉"], "same")
        right = source(2, ["鸡肉", "鸡肉粉"], "same")
        self.assertTrue(should_merge_formula_sources(left, right))
        self.assertEqual(len(cluster_formula_sources([left, right])), 1)

    def test_changed_head_ingredient_creates_new_version(self):
        left = source(1, ["鸡肉", "鸡肉粉", "鱼油", "南瓜"], "left")
        right = source(2, ["牛肉", "鸡肉粉", "鱼油", "南瓜"], "right")
        self.assertFalse(should_merge_formula_sources(left, right))
        self.assertEqual(len(cluster_formula_sources([left, right])), 2)


if __name__ == "__main__":
    unittest.main()
