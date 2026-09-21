import io
import unittest

from PIL import Image
from werkzeug.datastructures import FileStorage

from services import miniprogram_pet_image_service as service


class MiniProgramPetImageServiceTest(unittest.TestCase):
    def _image_file(self, *, size=(3000, 1500), image_format="PNG"):
        buffer = io.BytesIO()
        Image.new("RGB", size, "orange").save(buffer, format=image_format)
        buffer.seek(0)
        return FileStorage(stream=buffer, filename="pet.png", content_type="image/png")

    def test_prepare_image_normalizes_to_jpeg_and_limits_edge(self):
        content, width, height = service._prepare_image(self._image_file())
        self.assertEqual((width, height), (2048, 1024))
        with Image.open(io.BytesIO(content)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertEqual(result.mode, "RGB")

    def test_prepare_image_rejects_non_image(self):
        upload = FileStorage(stream=io.BytesIO(b"not an image"), filename="pet.jpg")
        with self.assertRaisesRegex(ValueError, "无法读取图片"):
            service._prepare_image(upload)

    def test_normalize_recognition_forces_confirmation_flags(self):
        result = service._normalize_recognition({
            "animal_type": {"value": "cat", "label": "猫咪", "confidence": 1.2},
            "breed": {"value": "英国短毛猫", "confidence": 0.82},
            "age": {"estimated_years": 2.5, "confidence": 0.4},
            "weight": {"estimated_kg": 4.5, "confidence": 0.2},
            "visible_health": {"observations": ["被毛整洁"]},
        })
        self.assertEqual(result["animal_type"]["confidence"], 1.0)
        self.assertTrue(result["age"]["requires_confirmation"])
        self.assertTrue(result["weight"]["requires_manual_input"])


if __name__ == "__main__":
    unittest.main()
