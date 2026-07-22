import unittest
from decimal import Decimal

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
            "is_default": "1",
        })

        self.assertEqual(result["user_id"], "u1")
        self.assertEqual(result["name"], "小灰")
        self.assertEqual(result["age_text"], "2岁3个月")
        self.assertEqual(result["weight_kg"], Decimal("4.60"))
        self.assertEqual(result["allergies"], ["鸡肉"])
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


if __name__ == "__main__":
    unittest.main()
