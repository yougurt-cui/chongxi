import unittest
from unittest.mock import patch

from services import soft_stool_assistant_service as service
from soft_stool_assistant import llm


class SoftStoolAssistantServiceTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
