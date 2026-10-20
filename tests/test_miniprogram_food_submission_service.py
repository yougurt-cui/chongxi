import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from werkzeug.datastructures import FileStorage

from services import miniprogram_food_submission_service as service


class MiniProgramFoodSubmissionServiceTest(unittest.TestCase):
    @patch("services.guardian_product_portrait_service.load_product_portraits")
    def test_portrait_exists_requires_current_chat_loadable_portrait(self, load):
        load.return_value = {}
        self.assertFalse(service._formula_portrait_exists(70))
        load.assert_called_once_with([70])

        load.reset_mock()
        load.return_value = {70: {"formula_id": 70}}
        self.assertTrue(service._formula_portrait_exists(70))
        load.assert_called_once_with([70])

    def test_status_label_does_not_call_approved_submission_published(self):
        self.assertEqual(service._status_label({
            "status": "active",
            "review_status": "approved",
            "publish_status": "processing",
        }), "审核通过，正在生成产品数据")
        self.assertEqual(service._status_label({
            "status": "active",
            "review_status": "approved",
            "publish_status": "published",
        }), "已收录")

    def test_serialize_does_not_expose_user_or_internal_task_ids(self):
        item = service._serialize({
            "id": "submission1",
            "user_id": "private-user",
            "orchestrator_task_id": "internal-task",
            "status": "active",
            "review_status": "pending",
            "publish_status": "unpublished",
            "recognition_status": "success",
        })
        self.assertNotIn("user_id", item)
        self.assertNotIn("orchestrator_task_id", item)

    def test_save_upload_validates_and_converts_image(self):
        buffer = io.BytesIO()
        Image.new("RGB", (200, 100), "white").save(buffer, "PNG")
        buffer.seek(0)
        upload = FileStorage(stream=buffer, filename="配料表.png", content_type="image/png")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(service.task_store, "UPLOAD_DIR", Path(directory)):
                path = service._save_upload(upload, product_name="测试产品", index=1)
                self.assertTrue(path.is_file())
                with Image.open(path) as image:
                    self.assertEqual(image.format, "JPEG")

    def test_save_upload_rejects_invalid_image(self):
        upload = FileStorage(stream=io.BytesIO(b"not-image"), filename="bad.jpg")
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(service.task_store, "UPLOAD_DIR", Path(directory)):
                with self.assertRaisesRegex(ValueError, "无法读取图片"):
                    service._save_upload(upload, product_name="测试产品", index=1)


if __name__ == "__main__":
    unittest.main()
