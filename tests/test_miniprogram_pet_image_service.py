import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_recognition_selects_single_highest_breed_and_random_sex(self):
        data = {'animal_type': {'value': 'cat'},
                'breed': {'value': '波斯猫', 'confidence': .8,
                          'candidates': [{'value': '布偶猫', 'confidence': .9},
                                         {'value': '比熊犬', 'confidence': 1}]}}
        for sex in ('male', 'female'):
            with patch.object(service.secrets, 'choice', return_value=sex):
                result = service._normalize_recognition(data)
            self.assertEqual(result['breed']['value'], '布偶猫')
            self.assertEqual(result['breed']['confidence'], .9)
            self.assertNotIn('candidates', result['breed'])
            self.assertEqual(result['sex']['value'], sex)
            self.assertEqual(result['sex']['source'], 'random')
        data['breed'] = {'value': '猫品种待确认', 'candidates': [
            {'value': '布偶猫', 'confidence': .4}]}
        self.assertEqual(service._normalize_recognition(data)['breed']['value'], '布偶猫')

    def test_recognition_prompt_uses_catalog_and_separate_appearance(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace()))
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='''{
            "animal_type":{"value":"cat","confidence":0.9},
            "breed":{"value":"Persian","confidence":0.8},
            "appearance":{"pattern":{"value":"calico","confidence":0.9}}
        }'''))])
        with patch.object(service, 'get_qwen_config', return_value={
                'api_key': 'test', 'base_url': 'https://example.test/v1', 'model': 'qwen3-vl-plus'}), \
             patch.object(service, 'OpenAI', return_value=client), \
             patch.object(client.chat.completions, 'create', create=True, return_value=response) as create:
            result, model = service.recognize_pet(self._image_file(size=(1600, 1200)).read())
        self.assertEqual(model, 'qwen3-vl-plus')
        self.assertEqual(result['breed']['value'], '波斯猫')
        self.assertEqual(result['appearance']['pattern']['value'], '三花')
        prompt = create.call_args.kwargs['messages'][0]['content'][1]['text']
        self.assertEqual(create.call_args.kwargs['max_tokens'], 180)
        self.assertNotIn('appearance', prompt)
        self.assertNotIn('candidates', prompt)
        import base64
        encoded = create.call_args.kwargs['messages'][0]['content'][0]['image_url']['url'].split(',')[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertEqual(image.size, (1024, 768))
        self.assertIn('中华田园猫', prompt)
        self.assertIn('马尔济斯犬', prompt)
        self.assertIn('三花、橘猫、奶牛花色、玳瑁属于花色', prompt)


if __name__ == "__main__":
    unittest.main()
