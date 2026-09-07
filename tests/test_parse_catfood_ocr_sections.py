import json
import unittest

from vendor.csv_mysql_labeling.src.parse_catfood_ocr import parse_ocr_json_fields


class CatfoodOcrIngredientSectionTest(unittest.TestCase):
    def test_additive_section_does_not_override_later_main_ingredients(self):
        full_text = """【添加剂组成】天然类固醇萨洒皂角苷、甘露寡糖、牛磺酸、维生素D3
ANALYTICAL CONSTITUENTS
产品成分分析保证值
粗蛋白质 ≥38.0%
产品信息
产品名称：鲜肉全价无谷猫粮-黑金系列
主要原料：鸡肉、鲜鸡肉、鸡肉粉、冻乳鸽肉、鲜鸡肝、鸡蛋
店铺 客服"""
        parsed = parse_ocr_json_fields(json.dumps({"full_text": full_text}, ensure_ascii=False))

        ingredients = parsed["ingredient_composition"] or ""
        self.assertIn("鸡肉", ingredients)
        self.assertIn("冻乳鸽肉", ingredients)
        self.assertNotIn("牛磺酸", ingredients)
        self.assertNotIn("维生素D3", ingredients)
        self.assertNotIn("ANALYTICAL", ingredients)

    def test_analytical_constituents_ends_main_ingredient_section(self):
        full_text = """主要原料：鲜鸡肉、鸡肉粉、鸡油
ANALYTICAL CONSTITUENTS
Crude protein 38%"""
        parsed = parse_ocr_json_fields(json.dumps({"full_text": full_text}, ensure_ascii=False))

        self.assertEqual(parsed["ingredient_composition"], "鲜鸡肉、鸡肉粉、鸡油")


if __name__ == "__main__":
    unittest.main()
