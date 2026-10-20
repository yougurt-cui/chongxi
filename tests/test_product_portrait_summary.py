import os
import unittest
from unittest.mock import MagicMock, patch

from main import create_app
from services.product_portrait_summary_service import (
    build_portrait,
    ensure_portrait_table,
    materialize_product_portrait,
)


class ProductPortraitRuleTest(unittest.TestCase):
    def test_schema_check_accepts_uppercase_information_schema_keys(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {"database_name": "protein_feature_platform"}
        cursor.fetchall.return_value = [
            {"COLUMN_NAME": "protein_quality_score"},
            {"COLUMN_NAME": "protein_quality_level"},
            {"COLUMN_NAME": "protein_quality_label"},
        ]

        ensure_portrait_table(conn)

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any(statement.startswith("ALTER TABLE") for statement in statements))
        conn.commit.assert_called_once_with()

    @patch("services.guardian_product_portrait_service.load_product_portraits")
    @patch("services.product_portrait_summary_service.rebuild_product_portraits")
    def test_pipeline_adapter_rebuilds_one_formula_and_validates_chat_loader(self, rebuild, load):
        rebuild.return_value = {"ok": True, "created": 1}
        load.return_value = {327: {
            "formula_id": 327, "source_feature_id": 8217, "summary": "五维画像",
        }}

        result = materialize_product_portrait({"formula_id": 327})

        rebuild.assert_called_once_with([327])
        load.assert_called_once_with([327])
        self.assertTrue(result["ok"])
        self.assertEqual(result["source_feature_id"], 8217)

    @patch("services.guardian_product_portrait_service.load_product_portraits", return_value={})
    @patch("services.product_portrait_summary_service.rebuild_product_portraits", return_value={"ok": True})
    def test_pipeline_adapter_fails_when_chat_cannot_load_written_portrait(self, _rebuild, _load):
        with self.assertRaisesRegex(RuntimeError, "校验失败"):
            materialize_product_portrait({"formula_id": 327})

    def test_builds_expected_dr25_summary(self):
        result = build_portrait({
            "id": 7255,
            "formula_id": 12,
            "sku_id": "皇家||DR25",
            "sku_name": "DR25",
            "brand_name": "皇家",
            "feature_version": "soft_v1",
            "protein_quality_score": "0.9190",
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
        self.assertEqual(result["dimensions"]["protein_quality"]["level"], "high")
        self.assertEqual(result["dimensions"]["protein"]["level"], "low")
        self.assertEqual(result["dimensions"]["fiber"]["level"], "high")
        self.assertEqual(result["dimensions"]["prebiotic"]["level"], "low")
        self.assertEqual(
            result["summary"],
            "蛋白质量较高，高纤维托底、益生元支持偏低，蛋白压力较低，但脂肪来源复杂且脂肪负担偏明显；碳水负担中低。",
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
