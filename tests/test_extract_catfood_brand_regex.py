import unittest

from vendor.csv_mysql_labeling.src.extract_catfood import _build_brand_re_from_values


class ExtractCatfoodBrandRegexTest(unittest.TestCase):
    def test_build_brand_re_from_standard_brand_values(self):
        brand_re = _build_brand_re_from_values(["皇家", "GO!", "皇家", "", None, "猫爸爸的厨房"])

        self.assertIn("皇家", brand_re)
        self.assertIn("GO!", brand_re)
        self.assertIn("猫爸爸的厨房", brand_re)
        self.assertEqual(brand_re.count("皇家"), 1)
        self.assertLess(brand_re.index("猫爸爸的厨房"), brand_re.index("皇家"))

    def test_build_brand_re_escapes_mysql_regex_metacharacters(self):
        brand_re = _build_brand_re_from_values(["A+B", "C(3)"])

        self.assertIn(r"A\+B", brand_re)
        self.assertIn(r"C\(3\)", brand_re)


if __name__ == "__main__":
    unittest.main()
