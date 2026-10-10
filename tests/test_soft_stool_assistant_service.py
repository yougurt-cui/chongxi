import unittest
from unittest.mock import patch

from services import soft_stool_assistant_service as service
from soft_stool_assistant import llm
from soft_stool_assistant import tools
from soft_stool_assistant import pipeline


class SoftStoolAssistantServiceTest(unittest.TestCase):
    def test_history_food_and_disease_are_asked_in_one_turn(self):
        state = pipeline.new_state("pet-1")
        state["context"]["history_food_names"] = ["皇家 BK34", "很长很长的鸡肉主粮名称"]
        state["context"]["disease_names"] = ["慢性肠胃炎"]
        group, question = pipeline.choose_next_question_group(state)
        self.assertEqual(group, "history_and_disease_group")
        self.assertIn("主粮", question)
        self.assertIn("其他疾病", question)
        self.assertNotIn("零食", question)

    def test_reference_pool_details_are_hidden_from_model_payload(self):
        analysis = {
            "reference_pool_version": "soft-v1",
            "reference_pool_size": 13,
            "confidence": "limited",
            "mechanisms": [{"mechanism": "脂肪消化负担"}],
        }
        self.assertEqual(
            llm._public_mechanism_analysis(analysis),
            {"mechanisms": [{"mechanism": "脂肪消化负担"}]},
        )

    def test_rule_extraction_collects_grouped_slots(self):
        state = {"slots": {}, "pattern": "with_baseline_diet"}
        result = llm.extract_user_info(
            "昨天换成百利高蛋白，大概一半，软便两天，没吐没血，吃饭精神都正常",
            state,
        )
        self.assertTrue(result["recent_diet_change"])
        self.assertEqual(result["new_product"], "百利高蛋白")
        self.assertEqual(result["change_ratio"], 0.5)
        self.assertEqual(result["soft_stool_days"], 2)
        self.assertFalse(result["vomiting"])
        self.assertFalse(result["blood_in_stool"])
        self.assertEqual(result["appetite"], "normal")
        self.assertEqual(result["energy"], "normal")

    @patch.object(service, "run_soft_stool_turn")
    @patch.object(service, "get_cat_profile")
    def test_turn_checks_ownership_and_returns_pipeline_state(self, get_profile, run_turn):
        get_profile.return_value = {"id": "pet-1"}
        run_turn.return_value = {
            "status": "need_more_info", "reply": "请补充", "pattern": "with_baseline_diet",
            "next_group": "symptom_group", "state": {"pet_id": "pet-1"},
        }
        result = service.handle_turn("user-1", {"pet_id": "pet-1", "message": "软便了"})
        get_profile.assert_called_once_with("user-1", "pet-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["pet_id"], "pet-1")
        self.assertIn("不能替代兽医诊断", result["disclaimer"])

    def test_rejects_state_from_another_pet(self):
        with patch.object(service, "get_cat_profile", return_value={"id": "pet-1"}):
            with self.assertRaisesRegex(ValueError, "不一致"):
                service.handle_turn("user-1", {
                    "pet_id": "pet-1", "message": "软便了", "state": {"pet_id": "pet-2"},
                })

    @patch.object(tools, "get_product_detail")
    @patch.object(tools, "get_pet_profile")
    def test_pet_without_profile_food_does_not_inherit_user_history(self, get_pet, get_product):
        get_pet.return_value = {
            "user_id": "user-1",
            "current_food": {
                "food_brand": None, "food_product": None,
                "food_product_id": None, "food_formula_id": None,
            },
        }
        result = tools.get_baseline_diet("pet-1")
        self.assertFalse(result["has_diet_data"])
        self.assertIsNone(result["source"])
        get_product.assert_not_called()


if __name__ == "__main__":
    unittest.main()
