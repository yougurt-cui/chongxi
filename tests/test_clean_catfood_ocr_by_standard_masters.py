import unittest

from scripts.clean_catfood_ocr_by_standard_masters import (
    PARSED_TABLE,
    RESULTS_TABLE,
    _backup_name,
    _safe_suffix,
)


class CleanCatfoodOcrByStandardMastersTest(unittest.TestCase):
    def test_safe_backup_suffix(self):
        self.assertEqual(_safe_suffix("2026-06-22 18:30:00"), "2026_06_22_18_30_00")

    def test_empty_backup_suffix_rejected(self):
        with self.assertRaises(ValueError):
            _safe_suffix("***")

    def test_backup_name_respects_mysql_identifier_limit(self):
        self.assertLessEqual(_backup_name(RESULTS_TABLE, "x" * 100).__len__(), 64)
        self.assertLessEqual(_backup_name(PARSED_TABLE, "x" * 100).__len__(), 64)


if __name__ == "__main__":
    unittest.main()
