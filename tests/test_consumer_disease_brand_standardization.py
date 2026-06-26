import unittest

from api.consumer_api import (
    _match_standard_brand_name,
    _normalized_brand_filter_value,
    _standardize_disease_brand,
)


class ConsumerDiseaseBrandStandardizationTest(unittest.TestCase):
    def test_match_standard_brand_from_alias(self):
        lookup = {
            "ziwi": "巅峰",
            "巅峰": "巅峰",
        }
        self.assertEqual(_match_standard_brand_name("ZIWI Peak", lookup), "巅峰")

    def test_match_standard_brand_preserves_multi_brand_events(self):
        lookup = {
            "巅峰": "巅峰",
            "皇家": "皇家",
        }
        self.assertEqual(_match_standard_brand_name("巅峰、皇家", lookup), "巅峰,皇家")

    def test_standardize_disease_brand_infers_from_context(self):
        lookup = {
            "巅峰": "巅峰",
        }
        candidate = {
            "brand_name": "",
            "search_keyword": "",
            "mentioned_brands": "巅峰",
            "review_text": "吃巅峰之后软便改善了",
        }
        self.assertEqual(_standardize_disease_brand(candidate, brand_lookup=lookup), "巅峰")

    def test_normalized_brand_filter_value_uses_each_part(self):
        self.assertEqual(_normalized_brand_filter_value("巅峰, 皇家"), "巅峰,皇家")


if __name__ == "__main__":
    unittest.main()
