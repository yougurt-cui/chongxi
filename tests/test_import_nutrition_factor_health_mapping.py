import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from scripts.import_nutrition_factor_health_mapping import read_rows


class NutritionFactorHealthWorkbookTests(unittest.TestCase):
    def make_workbook(self, rows):
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "mapping.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "nutrition_factor_health_mapping"
        sheet.append([
            "factor_code", "factor_name", "factor_status", "health_focus",
            "health_mechanism", "user_observation",
        ])
        for row in rows:
            sheet.append(row)
        workbook.save(path)
        self.addCleanup(temp.cleanup)
        return path

    def test_reads_and_assigns_stable_mapping_code(self):
        path = self.make_workbook([
            ["PROTEIN_QUALITY", "蛋白质量", "high", "肌肉维持", "机制", "观察"],
        ])
        first = read_rows(path)
        second = read_rows(path)
        self.assertEqual(first[0]["mapping_code"], second[0]["mapping_code"])
        self.assertEqual(first[0]["source_row_number"], 2)

    def test_rejects_unknown_factor(self):
        path = self.make_workbook([
            ["UNKNOWN", "未知", "high", "关注点", "机制", "观察"],
        ])
        with self.assertRaisesRegex(ValueError, "factor_code 无效"):
            read_rows(path)

    def test_rejects_duplicate_identity(self):
        row = ["FAT_BURDEN", "脂肪负担", "high", "软便", "机制", "观察"]
        path = self.make_workbook([row, row])
        with self.assertRaisesRegex(ValueError, "重复映射"):
            read_rows(path)


if __name__ == "__main__":
    unittest.main()
