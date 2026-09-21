import unittest

from services import miniprogram_chat_service as service


class MiniProgramChatServiceTest(unittest.TestCase):
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
        self.assertEqual(flow["question"]["response_type"], "multi_select")

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


if __name__ == "__main__":
    unittest.main()
