import unittest
from decimal import Decimal
from unittest.mock import patch

from services import miniprogram_cat_profile_service as service


class MiniProgramCatProfileServiceTest(unittest.TestCase):
    def test_normalize_payload_accepts_publish_page_cat_fields(self):
        result = service._normalize_payload({
            "user_id": "u1",
            "name": " 小灰 ",
            "breed": "英短蓝猫",
            "age": "2岁3个月",
            "age_months": 27,
            "weight": "4.6",
            "sex": "unknown",
            "neutered": True,
            "allergies": ["鸡肉", "鸡肉", ""],
            "diseases": ["黑下巴"],
            "symptoms": ["软便"],
            "diet": {"brand": " 皇家 ", "product": " 肠胃舒适成猫粮 "},
            "animal_type": "cat",
            "health_status": ["状态正常"],
            "avatar_image_id": "image123",
            "is_default": "1",
        })

        self.assertEqual(result["user_id"], "u1")
        self.assertEqual(result["name"], "小灰")
        self.assertEqual(result["age_text"], "2岁3个月")
        self.assertEqual(result["weight_kg"], Decimal("4.60"))
        self.assertEqual(result["allergies"], ["鸡肉"])
        self.assertEqual(result["food_brand"], "皇家")
        self.assertEqual(result["food_product"], "肠胃舒适成猫粮")
        self.assertEqual(result["animal_type"], "cat")
        self.assertEqual(result["health_status"], ["状态正常"])
        self.assertEqual(result["avatar_image_id"], "image123")
        self.assertEqual(result["is_default"], 1)

    def test_user_id_is_required(self):
        with self.assertRaisesRegex(ValueError, "user_id 不能为空"):
            service._normalize_payload({"name": "小灰"})

    def test_name_is_required(self):
        with self.assertRaisesRegex(ValueError, "name 不能为空"):
            service._normalize_payload({"user_id": "u1", "name": ""})

    def test_weight_range_is_validated(self):
        with self.assertRaisesRegex(ValueError, "weight_kg 必须在"):
            service._normalize_payload({"user_id": "u1", "name": "小灰", "weight_kg": "40"})

    def test_partial_payload_only_normalizes_present_fields(self):
        result = service._normalize_payload({"weight_kg": "5.25"}, partial=True)
        self.assertEqual(result, {"weight_kg": Decimal("5.25")})

    def test_partial_diet_update_only_normalizes_present_diet_field(self):
        result = service._normalize_payload({"diet": {"brand": "渴望"}}, partial=True)
        self.assertEqual(result, {"food_brand": "渴望"})

    def test_flat_food_fields_are_supported(self):
        result = service._normalize_payload({
            "food_brand": "爱肯拿",
            "food_product": "室内开胃配方",
        }, partial=True)
        self.assertEqual(result, {
            "food_brand": "爱肯拿",
            "food_product": "室内开胃配方",
        })

    def test_diet_must_be_an_object(self):
        with self.assertRaisesRegex(ValueError, "diet 必须是对象"):
            service._normalize_payload({"diet": "皇家"}, partial=True)

    def test_serialize_includes_flat_and_nested_diet_fields(self):
        result = service._serialize({
            "food_brand": "皇家",
            "food_product": "肠胃舒适",
            "avatar_image_id": "image123",
        })
        self.assertEqual(result["food_brand"], "皇家")
        self.assertEqual(result["food_product"], "肠胃舒适")
        self.assertEqual(result["diet"], {
            "brand": "皇家", "product": "肠胃舒适",
            "product_id": None, "formula_id": None,
        })
        self.assertEqual(result["avatar_url"], "/api/miniprogram/pet-images/image123")

    def test_animal_type_is_validated(self):
        with self.assertRaisesRegex(ValueError, "animal_type 仅支持"):
            service._normalize_payload({"animal_type": "rabbit"}, partial=True)

    def test_food_product_and_formula_ids_are_normalized_together(self):
        result = service._normalize_payload({
            "diet": {"product_id": "12", "formula_id": "34"},
        }, partial=True)
        self.assertEqual(result, {"food_product_id": 12, "food_formula_id": 34})

        with self.assertRaisesRegex(ValueError, "必须同时提供"):
            service._normalize_payload({"food_formula_id": 34}, partial=True)

    def test_formula_snapshot_uses_canonical_catalog_names(self):
        data = {"food_product_id": 12, "food_formula_id": 34, "food_brand": "raw"}
        with patch.object(service, "_resolve_food_formula", return_value={
            "standard_brand_name": "皇家",
            "standard_product_name": "肠胃舒适成猫粮",
            "display_name": "肠胃舒适",
        }):
            service._apply_food_formula_snapshot(data)
        self.assertEqual(data["food_brand"], "皇家")
        self.assertEqual(data["food_product"], "肠胃舒适")

    def test_create_profile_persists_product_and_formula_ids(self):
        class Cursor:
            def __init__(self):
                self.insert = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, params):
                if "INSERT INTO" in sql:
                    self.insert = (sql, params)

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                return None

        connection = Connection()
        payload = {
            "user_id": "user-1", "name": "小灰",
            "food_product_id": 12, "food_formula_id": 34,
        }
        with (
            patch.object(service, "init_miniprogram_cat_profile_tables"),
            patch.object(service, "_connect_app", return_value=connection),
            patch.object(service, "_resolve_food_formula", return_value={
                "standard_brand_name": "皇家", "standard_product_name": "肠胃舒适",
                "display_name": "肠胃舒适",
            }),
            patch.object(service, "get_cat_profile", return_value={"id": "pet-1"}),
        ):
            service.create_cat_profile(payload)

        sql, params = connection.cursor_instance.insert
        self.assertEqual(sql.count("%s"), len(params))
        self.assertIn("food_product_id,food_formula_id", sql)
        self.assertIn(12, params)
        self.assertIn(34, params)

    def test_delete_profile_marks_record_deleted_and_promotes_new_default(self):
        class Cursor:
            def __init__(self):
                self.statements = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, params):
                self.statements.append((" ".join(sql.split()), params))

            def fetchone(self):
                return {"is_default": 1}

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.committed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.committed = True

        connection = Connection()
        with (
            patch.object(service, "init_miniprogram_cat_profile_tables"),
            patch.object(service, "_connect_app", return_value=connection),
            patch.object(service, "_now", return_value="2026-09-25 12:00:00"),
        ):
            result = service.delete_cat_profile("user-1", "pet-1")

        statements = connection.cursor_instance.statements
        self.assertTrue(connection.committed)
        self.assertIn("LIMIT 1 FOR UPDATE", statements[0][0])
        self.assertIn("status='deleted'", statements[1][0])
        self.assertIn("deleted_at=%s", statements[1][0])
        self.assertIn("ORDER BY updated_at DESC LIMIT 1", statements[2][0])
        self.assertEqual(result, {
            "ok": True,
            "id": "pet-1",
            "deleted_at": "2026-09-25 12:00:00",
        })

    def test_delete_profile_returns_not_found_for_inactive_profile(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, _sql, _params):
                return None

            def fetchone(self):
                return None

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self):
                return Cursor()

        with (
            patch.object(service, "init_miniprogram_cat_profile_tables"),
            patch.object(service, "_connect_app", return_value=Connection()),
        ):
            with self.assertRaisesRegex(LookupError, "猫咪档案不存在"):
                service.delete_cat_profile("user-1", "pet-1")


if __name__ == "__main__":
    unittest.main()
