import os
import unittest
from unittest.mock import patch

from main import create_app
from services.product_portrait_summary_service import build_portrait


class ProductPortraitRuleTest(unittest.TestCase):
    def test_builds_expected_dr25_summary(self):
        result = build_portrait({
            "id": 7255,
            "formula_id": 12,
            "sku_id": "皇家||DR25",
            "sku_name": "DR25",
            "brand_name": "皇家",
            "feature_version": "soft_v1",
            "protein_score": "0.2550",
            "fat_score": "0.6870",
            "fiber_score": "4.4600",
            "carb_score": "1.5600",
            "prebiotic_score": "0.6200",
            "fat_structure_type": "动物+植物+鱼油复合型",
            "fat_reason_tags": "动物脂肪负担偏高,脂肪来源较复杂",
            "p_level": "高P",
            "q_level": "低Q",
            "pq_combo_name": "高纤维纯托底型",
        })
        self.assertEqual(result["dimensions"]["protein"]["level"], "low")
        self.assertEqual(result["dimensions"]["fiber"]["level"], "high")
        self.assertEqual(result["dimensions"]["prebiotic"]["level"], "low")
        self.assertEqual(
            result["summary"],
            "高纤维托底、益生元支持偏低，蛋白压力较低，但脂肪来源复杂且脂肪负担偏明显；碳水负担中低。",
        )

    def test_missing_values_are_not_invented(self):
        result = build_portrait({"id": 1, "formula_id": 2, "feature_version": "soft_v1"})
        self.assertEqual(result["dimensions"]["protein"]["level"], "missing")
        self.assertIn("资料不足", result["summary"])


class ProductPortraitApiTest(unittest.TestCase):
    def setUp(self):
        os.environ["CHONGXI_SESSION_SECRET"] = "test-secret"
        os.environ["MINIPROGRAM_ADMIN_TOKEN"] = "test-admin-token"
        self.client = create_app().test_client()
        self.headers = {"X-Admin-Token": "test-admin-token"}

    def test_requires_admin(self):
        response = self.client.post("/api/data-pipeline/product-portraits/rebuild", json={})
        self.assertEqual(response.status_code, 401)

    def test_rejects_non_boolean_flags(self):
        response = self.client.post(
            "/api/data-pipeline/product-portraits/rebuild",
            json={"dry_run": "true"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)

    @patch("api.product_portrait_api.rebuild_all_product_portraits")
    def test_rebuilds_full_library(self, rebuild):
        rebuild.return_value = {"ok": True, "total": 105, "created": 105}
        response = self.client.post(
            "/api/data-pipeline/product-portraits/rebuild",
            json={"dry_run": True}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        rebuild.assert_called_once_with(feature_version="soft_v1", force=False, dry_run=True)


if __name__ == "__main__":
    unittest.main()
