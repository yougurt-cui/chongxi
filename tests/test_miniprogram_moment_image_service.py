import unittest
from io import BytesIO

from werkzeug.datastructures import FileStorage

from services import miniprogram_moment_image_service as service


def make_file(filename: str, content_type: str = "image/jpeg") -> FileStorage:
    return FileStorage(stream=BytesIO(b"image-bytes"), filename=filename, content_type=content_type)


class MiniProgramMomentImageServiceTest(unittest.TestCase):
    def test_upload_file_accepts_supported_image(self):
        original, suffix, content_type = service._normalize_upload_file(make_file("cat.jpg"))
        self.assertEqual(original, "cat.jpg")
        self.assertEqual(suffix, ".jpg")
        self.assertEqual(content_type, "image/jpeg")

    def test_upload_file_rejects_unknown_extension(self):
        with self.assertRaisesRegex(ValueError, "仅支持"):
            service._normalize_upload_file(make_file("cat.txt", "text/plain"))

    def test_upload_file_rejects_wrong_content_type(self):
        with self.assertRaisesRegex(ValueError, "仅支持"):
            service._normalize_upload_file(make_file("cat.jpg", "text/plain"))

    def test_upload_file_accepts_octet_stream_with_image_extension(self):
        original, suffix, content_type = service._normalize_upload_file(make_file("cat.jpg", "application/octet-stream"))
        self.assertEqual(original, "cat.jpg")
        self.assertEqual(suffix, ".jpg")
        self.assertEqual(content_type, "image/jpeg")

    def test_user_id_is_required(self):
        with self.assertRaisesRegex(ValueError, "user_id 不能为空"):
            service._clean_user_id("")

    def test_safe_segment_removes_path_chars(self):
        self.assertEqual(service._safe_segment("../u/1"), "u_1")


if __name__ == "__main__":
    unittest.main()
