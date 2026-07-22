import unittest
from decimal import Decimal

from services import miniprogram_moment_service as service


class MiniProgramMomentServiceTest(unittest.TestCase):
    def test_categories_are_fixed(self):
        result = service.list_moment_categories()
        self.assertEqual([item["code"] for item in result["items"]], ["SLEEP", "FUNNY", "CHIN", "VOMIT", "STOOL"])

    def test_normalize_publish_page_payload(self):
        result = service._normalize_payload({
            "user_id": "u1",
            "category": "睡姿大赏",
            "content": "睡成这样，还能找到头？",
            "images": ["https://example.com/cat.jpg"],
            "breed": "英短蓝猫",
            "age": "2岁3个月",
            "weight": "4.6",
            "visibility": "public",
        })

        self.assertEqual(result["category_code"], "SLEEP")
        self.assertEqual(result["category_name"], "睡姿大赏")
        self.assertEqual(result["title"], "睡成这样，还能找到头？")
        self.assertEqual(result["images"], [{"url": "https://example.com/cat.jpg"}])
        self.assertEqual(result["weight_kg"], Decimal("4.60"))

    def test_normalize_publish_page_accepts_weight_range_text(self):
        result = service._normalize_payload({
            "user_id": "u1",
            "category_code": "SLEEP",
            "content": "睡姿记录",
            "images": ["https://example.com/cat.jpg"],
            "weight": "4-5kg",
        })

        self.assertEqual(result["weight_kg"], Decimal("4.00"))

    def test_rejects_unknown_category(self):
        with self.assertRaisesRegex(ValueError, "category_code 仅支持"):
            service._normalize_payload({
                "user_id": "u1",
                "category_code": "OTHER",
                "content": "hello",
                "images": ["https://example.com/cat.jpg"],
            })

    def test_rejects_empty_images(self):
        with self.assertRaisesRegex(ValueError, "images 不能为空"):
            service._normalize_payload({
                "user_id": "u1",
                "category_code": "FUNNY",
                "content": "hello",
                "images": [],
            })

    def test_rejects_temp_image_paths(self):
        with self.assertRaisesRegex(ValueError, "图片需要先上传"):
            service._normalize_payload({
                "user_id": "u1",
                "category_code": "FUNNY",
                "content": "hello",
                "images": ["http://tmp/demo.png"],
            })

    def test_visibility_is_limited(self):
        with self.assertRaisesRegex(ValueError, "visibility 仅支持"):
            service._normalize_payload({
                "user_id": "u1",
                "category_code": "FUNNY",
                "content": "hello",
                "images": ["https://example.com/cat.jpg"],
                "visibility": "friends",
            })

    def test_serialize_includes_liked_and_comments_for_detail(self):
        result = service._serialize({
            "id": "p1",
            "user_id": "u1",
            "category_code": "FUNNY",
            "category_name": "搞笑日常",
            "title": "title",
            "content": "content",
            "images_json": "[]",
            "visibility": "public",
            "like_count": 2,
            "comment_count": 1,
            "liked": True,
            "comments": [{"id": "c1"}],
        })

        self.assertTrue(result["liked"])
        self.assertEqual(result["likes"], 2)
        self.assertEqual(result["commentCount"], 1)
        self.assertEqual(result["comments"], [{"id": "c1"}])

    def test_serialize_comment_masks_anonymous_author(self):
        result = service._serialize_comment({
            "id": "c1",
            "post_id": "p1",
            "user_id": "u1",
            "content": "太可爱了",
            "author_name": "小王",
            "author_avatar": "avatar.png",
            "anonymous": 1,
            "like_count": 3,
        })

        self.assertEqual(result["author"], "匿名铲屎官")
        self.assertEqual(result["avatar"], "")
        self.assertEqual(result["likes"], 3)

    def test_comment_id_is_required(self):
        with self.assertRaisesRegex(ValueError, "comment_id 不能为空"):
            service._clean_comment_id("")

    def test_publish_payload_creates_cat_profile_when_profile_id_missing(self):
        class Cursor:
            def __init__(self):
                self.statements = []

            def execute(self, sql, params=None):
                self.statements.append((sql, params))

            def fetchone(self):
                return None

        cursor = Cursor()
        data = service._normalize_payload({
            "user_id": "u1",
            "category_code": "SLEEP",
            "content": "睡姿记录",
            "images": [{"url": "/api/miniprogram/moment-images/f1"}],
            "breed": "英短蓝猫",
            "sex": "male",
            "age": "2岁",
            "weight": "4.6",
        })

        profile = service._ensure_cat_profile_for_moment(cursor, data, "2026-07-19 12:00:00")

        self.assertTrue(data["cat_profile_id"])
        self.assertEqual(profile["name"], "我的猫咪")
        self.assertEqual(profile["breed"], "英短蓝猫")
        self.assertEqual(profile["sex"], "male")
        self.assertTrue(any("INSERT INTO miniprogram_cat_profile" in sql for sql, _ in cursor.statements))


if __name__ == "__main__":
    unittest.main()
