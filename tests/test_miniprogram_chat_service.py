import unittest
from unittest.mock import patch

from app_config import get_chat_model_config
from services import miniprogram_chat_service as service


class MiniProgramChatServiceTest(unittest.TestCase):
    @patch.dict("os.environ", {"CHAT_MODEL_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "test-key"}, clear=False)
    def test_deepseek_chat_config(self):
        config = get_chat_model_config()
        self.assertEqual(config["provider"], "deepseek")
        self.assertEqual(config["base_url"], "https://api.deepseek.com")
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertEqual(config["api_key"], "test-key")

    def test_deepseek_disables_thinking_for_chat(self):
        self.assertEqual(
            service._provider_options({"provider": "deepseek"}),
            {"extra_body": {"thinking": {"type": "disabled"}}},
        )
        self.assertEqual(service._provider_options({"provider": "qwen"}), {})

    def test_fallback_extracts_symptom_and_secondary_food_switch(self):
        result = service._fallback_extraction("最近软便，想了解换粮", service._default_state("c1"))
        self.assertEqual(result["primary_intent"], "symptom_consult")
        self.assertEqual(result["secondary_intent"], "food_switch")
        self.assertEqual(result["slots"]["symptom"], "软便")

    def test_symptom_flow_asks_warning_signs_first(self):
        state = service._default_state("c1")
        state["primary_intent"] = "symptom_consult"
        state["slots"] = {"symptom": "软便"}
        flow = service._evaluate_flow(state, {})
        self.assertEqual(flow["status"], "need_more_info")
        self.assertEqual(flow["next_slot"], "warning_signs")
        self.assertEqual(flow["question"]["response_type"], "text")
        self.assertEqual(flow["question"]["options"], [])
        self.assertIn("陪你一起理一理", flow["question"]["text"])

    def test_symptom_flow_uses_two_combined_clarification_rounds(self):
        state = service._default_state("c1")
        state["primary_intent"] = "symptom_consult"
        state["slots"] = {"symptom": "软便", "warning_signs": ["none"]}
        state["followup_count"] = 1
        flow = service._evaluate_flow(state, {})
        self.assertEqual(flow["status"], "need_more_info")
        self.assertEqual(flow["next_slot"], "symptom_context")
        self.assertTrue(flow["question"]["text"].startswith("好的，了解了。"))
        self.assertIn("持续多久", flow["question"]["text"])
        self.assertIn("换粮", flow["question"]["text"])

        state["followup_count"] = 2
        flow = service._evaluate_flow(state, {})
        self.assertEqual(flow["status"], "answer_with_limited_info")

    def test_risk_interrupt_precedes_followup(self):
        state = service._default_state("c1")
        state["primary_intent"] = "symptom_consult"
        state["slots"] = {"symptom": "软便", "warning_signs": ["blood_in_stool"]}
        flow = service._evaluate_flow(state, {})
        self.assertEqual(flow["status"], "risk_interrupt")
        self.assertEqual(flow["risk"]["level"], "high")

    def test_current_food_context_satisfies_food_switch_slot(self):
        state = service._default_state("c1")
        state["primary_intent"] = "food_switch"
        state["slots"] = {"switch_reason": "digestive", "target_requirement": "低敏"}
        flow = service._evaluate_flow(state, {"current_food": {"product_name": "BK34"}})
        self.assertEqual(flow["status"], "ready_to_answer")

    def test_model_json_accepts_fenced_output(self):
        result = service._parse_model_json('```json\n{"primary_intent":"food_switch"}\n```')
        self.assertEqual(result["primary_intent"], "food_switch")

    def test_three_point_answer_has_exactly_three_short_lines(self):
        text = service._three_point_text({
            "context": "最近换粮可能让肠胃暂时不适应。",
            "care": "建议先暂停新零食，让饮食保持稳定。",
            "watch": "如果出现便血或精神变差，请联系宠物医院。",
        })
        self.assertEqual(len(text.splitlines()), 3)
        self.assertNotIn("建议", text)
        self.assertTrue(text.startswith("1. "))

    def test_warning_signs_are_understood_from_natural_text(self):
        self.assertEqual(service._warning_signs_from_text("没有这些情况，精神挺好的"), ["none"])
        self.assertEqual(
            service._warning_signs_from_text("今天吐了，而且有点没精神"),
            ["vomiting", "poor_mental_status"],
        )

    def test_daily_records_are_limited_and_normalized(self):
        records = service._normalize_daily_records([{
            "day": "2026-09-24", "water_ml": 180, "food_g": 60, "stool_count": 2,
            "litter_notes": [{"time": "08:30", "count": 1, "shape": "软"}],
        }])
        self.assertEqual(records[0]["water_ml"], 180)
        self.assertEqual(records[0]["stool_notes"][0]["shape"], "软")

    def test_plain_text_clarification_has_no_selection_interaction(self):
        question = service.SLOT_QUESTIONS["warning_signs"]
        self.assertEqual(question["response_type"], "text")
        self.assertFalse(question["options"])


if __name__ == "__main__":
    unittest.main()
