import json
import unittest
from unittest.mock import patch

from services import pet_content_operations_service as service


class PetContentOperationsServiceTest(unittest.TestCase):
    def test_build_prompt_uses_material_without_copying_brand(self):
        prompt = service.build_image_prompt({
            "sub_category": "逗猫棒",
            "product_type": "羽毛逗猫棒",
            "pet_action": "猫咪跳跃扑捉",
            "usage_scene": "客厅",
        }, "自然生活方式摄影")
        self.assertIn("猫咪跳跃扑捉", prompt)
        self.assertIn("竖版 3:4", prompt)
        self.assertIn("不复制", prompt)
        self.assertIn("【物理结构硬约束】", prompt)
        self.assertIn("不得悬空", prompt)
        self.assertIn("禁止漂浮、穿模", prompt)

    def test_serializers_parse_json_fields(self):
        material = service._serialize_material({"visual_tags": '["猫咪", "玩具"]'})
        task = service._serialize_task({"hashtags_json": '["猫咪"]'})
        self.assertEqual(material["visual_tags"], ["猫咪", "玩具"])
        self.assertEqual(task["hashtags"], ["猫咪"])
        self.assertNotIn("hashtags_json", task)

    def test_clean_limits_text(self):
        self.assertEqual(service._clean(" abc ", 2), "ab")

    def test_collection_command_uses_argument_list_without_shell(self):
        command, config = service._build_collection_command({
            "platform": "amazon", "category": "玩具", "sub_category": "漏食玩具",
            "keyword": "automatic cat treat dispenser", "limit": 8, "use_vision": False,
        })
        self.assertEqual(command[0], service.sys.executable)
        self.assertIn("--platforms", command)
        self.assertIn("amazon", command)
        self.assertIn("--keyword", command)
        self.assertIn("automatic cat treat dispenser", command)
        self.assertIn("--no-vision", command)
        self.assertEqual(config["item_limit"], 8)

    def test_collection_command_rejects_invalid_platform_and_limit(self):
        with self.assertRaisesRegex(ValueError, "platform"):
            service._build_collection_command({"platform": "unknown"})
        with self.assertRaisesRegex(ValueError, "1 到 20"):
            service._build_collection_command({"platform": "amazon", "limit": 100})

    def test_full_collection_uses_all_catalog_defaults(self):
        command, config = service._build_collection_command({"platform": "amazon", "limit": 3})
        self.assertNotIn("--category", command)
        self.assertNotIn("--sub-category", command)
        self.assertNotIn("--keyword", command)
        self.assertEqual(config["item_limit"], 3)

    def test_generate_content_task_enqueues_without_running_inline(self):
        material = {"id": 7, "sub_category": "逗猫棒"}
        with (
            patch.object(service, "get_material", return_value=material),
            patch.object(service, "_create_task", return_value="task-1"),
            patch.object(service, "get_task", return_value={"id": "task-1", "status": "pending"}),
            patch.object(service._generation_executor, "submit") as submit,
        ):
            result = service.generate_content_task(7)
        self.assertEqual(result["item"]["status"], "pending")
        submitted = submit.call_args.args
        self.assertEqual(submitted[:2], (service._run_content_generation, "task-1"))
        self.assertEqual(submitted[2], material)
        self.assertEqual(submitted[3], "")

    def test_creative_plan_preserves_llm_structure_and_anchors(self):
        class FakeClient:
            def chat(self, **kwargs):
                self.kwargs = kwargs
                return json.dumps({
                    "common_layer": ["真实宠物摄影", "竖版3:4"],
                    "difference_layer": ["环形轨道", "猫爪拨球"],
                    "exclusion_layer": ["不要羽毛逗猫棒", "不要暖色木地板"],
                    "final_prompt": "一只猫用前爪拨动环形轨道里的球，真实摄影。",
                }, ensure_ascii=False)

        client = FakeClient()
        prompt = service.build_image_prompt({
            "sub_category": "轨道球", "product_type": "环形轨道球玩具",
            "product_shape": "环形轨道内嵌小球", "pet_action": "猫爪拨球",
            "interaction_type": "球沿轨道滚动，猫继续追逐",
        }, llm_client=client)
        self.assertIn("环形轨道", prompt)
        self.assertIn("猫爪拨球", prompt)
        self.assertIn("共性层", prompt)
        self.assertIn("差异层", prompt)
        self.assertIn("互斥层", prompt)
        self.assertIn("不得改变产品类别或互动机制", prompt)
        self.assertIn("产品形状/功能结构", client.kwargs["user_prompt"])

    def test_different_materials_receive_different_visual_routes(self):
        first = service.build_image_prompt({
            "id": 20, "product_type": "电动毛绒猫玩具", "pet_action": "猫咪观察玩具",
        })
        second = service.build_image_prompt({
            "id": 22, "product_type": "自动逗猫棒", "pet_action": "猫咪扑击玩具",
        })
        self.assertNotEqual(first, second)
        self.assertIn("低机位动态抓拍", first)
        self.assertIn("近距离特写", second)
        self.assertIn("不得使用橘猫、浅木地板", first)
        self.assertIn("可以制造并安全使用", first)

    def test_text_evidence_corrects_feeder_misclassified_as_toy(self):
        prompt = service.build_image_prompt({
            "id": 12,
            "title": "Automatic Cat Treat Dispenser and Snack Launcher",
            "search_keyword": "automatic feeder",
            "sub_category": "自动互动玩具",
            "product_type": "普通逗猫玩具",
            "pet_action": "猫追逐设备",
        })
        self.assertIn("【产品主属性】", prompt)
        self.assertIn("自动投食器 / 自动零食发射器", prompt)
        self.assertIn("【功能机制】", prompt)
        self.assertIn("储存宠物零食", prompt)
        self.assertIn("【视觉证据】", prompt)
        self.assertIn("出粮口", prompt)
        self.assertIn("【互斥限制】", prompt)
        self.assertIn("不要将其表现为追逐扑击类互动玩具", prompt)
        self.assertNotIn("本产品的核心类别是普通逗猫玩具", prompt)

    def test_generate_image_calls_volcengine_ark(self):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.headers = {"x-tt-logid": "ark-request-1"}
        response.json.return_value = {"data": [{"url": "https://example.com/image.png"}]}
        with (
            patch.object(service, "get_ark_image_config", return_value={
                "api_key": "ark-key",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "model": "doubao-seedream-4-0-250828",
            }),
            patch.object(service.requests, "post", return_value=response) as post,
        ):
            request_id, image_url = service._generate_image("一只玩逗猫棒的猫")

        self.assertEqual(request_id, "ark-request-1")
        self.assertEqual(image_url, "https://example.com/image.png")
        call = post.call_args
        self.assertEqual(call.args[0], "https://ark.cn-beijing.volces.com/api/v3/images/generations")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer ark-key")
        self.assertEqual(call.kwargs["json"]["size"], "1728x2304")
        self.assertEqual(call.kwargs["json"]["sequential_image_generation"], "disabled")
        self.assertFalse(call.kwargs["json"]["stream"])
        self.assertTrue(call.kwargs["json"]["watermark"])

    def test_generate_image_requires_ark_api_key(self):
        with patch.object(service, "get_ark_image_config", return_value={
            "api_key": "", "base_url": "https://ark.example/api/v3", "model": "seedream",
        }):
            with self.assertRaisesRegex(RuntimeError, "ARK_API_KEY"):
                service._generate_image("prompt")

    def test_generate_image_explains_model_not_open(self):
        response = unittest.mock.Mock()
        response.status_code = 404
        response.json.return_value = {
            "error": {"code": "ModelNotOpen", "message": "account has not activated model"},
        }
        with (
            patch.object(service, "get_ark_image_config", return_value={
                "api_key": "ark-key", "base_url": "https://ark.example/api/v3",
                "model": "doubao-seedream-4-0-250828",
            }),
            patch.object(service.requests, "post", return_value=response),
        ):
            with self.assertRaisesRegex(RuntimeError, "开通管理"):
                service._generate_image("prompt")

    def test_generate_copy_uses_fallback_when_model_returns_empty_fields(self):
        response = unittest.mock.Mock()
        response.choices = [unittest.mock.Mock(message=unittest.mock.Mock(content='{"title":"","content":"","hashtags":[]}'))]
        client = unittest.mock.Mock()
        client.chat.completions.create.return_value = response
        with (
            patch.object(service, "get_qwen_config", return_value={
                "api_key": "key", "base_url": "https://example.com/v1", "model": "qwen-plus",
            }),
            patch.object(service, "OpenAI", return_value=client),
        ):
            copy = service._generate_copy({"sub_category": "轨道球", "pet_action": "用爪子拨球"})
        self.assertTrue(copy["title"])
        self.assertTrue(copy["content"])
        self.assertTrue(copy["hashtags"])

    def test_approve_rejects_empty_copy(self):
        with patch.object(service, "get_task", return_value={
            "id": "task-1", "status": "draft", "title": None, "content": None,
            "generated_image_url": "https://example.com/image.jpg",
        }):
            with self.assertRaisesRegex(ValueError, "标题、正文和图片"):
                service.approve_task("task-1")

    def test_publish_uses_official_miniprogram_identity(self):
        task = {
            "id": "task-1", "status": "approved", "publish_status": "unpublished",
            "material_id": 7, "title": "猫咪玩球", "content": "今天玩得很开心。",
            "hashtags": ["猫咪"], "generated_image_url": "https://example.com/image.jpg",
        }
        created = {"item": {"id": "post-1"}}
        with (
            patch.object(service, "get_task", side_effect=[task, {**task, "publish_status": "published"}]),
            patch.object(service, "_update_task"),
            patch("services.miniprogram_moment_service.create_moment", return_value=created) as create,
            patch.object(service, "_connect") as connect,
        ):
            result = service.publish_task("task-1")
        payload = create.call_args.args[0]
        self.assertEqual(payload["user_id"], service.OFFICIAL_USER_ID)
        self.assertEqual(payload["author_name"], "宠析官方")
        self.assertEqual(payload["visibility"], "public")
        self.assertEqual(result["item"]["publish_status"], "published")


if __name__ == "__main__":
    unittest.main()
