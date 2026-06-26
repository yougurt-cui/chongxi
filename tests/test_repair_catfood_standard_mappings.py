import unittest
from unittest.mock import Mock

from scripts.repair_catfood_standard_mappings import _mapping_has_exact_product_identity


class RepairStandardMappingsTest(unittest.TestCase):
    def test_exact_standard_name_retains_product(self):
        cursor = Mock()
        cursor.fetchone.return_value = {
            "standard_product_name": "HALO 全价宠物食品成年期猫粮",
            "display_name": "成猫粮",
        }
        cursor.fetchall.return_value = []
        self.assertTrue(
            _mapping_has_exact_product_identity(
                cursor,
                {
                    "product_id": 32,
                    "raw_product_name": "HALO 全价宠物食品成年期猫粮",
                },
            )
        )

    def test_different_ocr_name_does_not_retain_product(self):
        cursor = Mock()
        cursor.fetchone.return_value = {
            "standard_product_name": "九种肉",
            "display_name": "九种肉",
        }
        cursor.fetchall.return_value = []
        self.assertFalse(
            _mapping_has_exact_product_identity(
                cursor,
                {
                    "product_id": 91,
                    "raw_product_name": "鲜肉高蛋白美毛 15.13.13",
                },
            )
        )


if __name__ == "__main__":
    unittest.main()
