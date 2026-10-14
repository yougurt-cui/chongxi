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

    def test_general_guardian_does_not_require_a_warning_sign_questionnaire(self):
        state = service._default_state("c1")
        state["primary_intent"] = "symptom_consult"
        state["slots"] = {"symptom": "软便"}
        flow = service._evaluate_flow(state, {})
        self.assertEqual(flow["status"], "ready_to_answer")
        self.assertEqual(flow["missing_slots"], [])

    def test_missing_symptom_details_do_not_hold_up_guardian_advice(self):
        state = service._default_state("c1")
        state["primary_intent"] = "symptom_consult"
        state["slots"] = {"symptom": "软便", "warning_signs": ["none"]}
        state["followup_count"] = 1
        flow = service._evaluate_flow(state, {})
        self.assertEqual(flow["status"], "ready_to_answer")

        state["followup_count"] = 2
        flow = service._evaluate_flow(state, {})
        self.assertEqual(flow["status"], "ready_to_answer")

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

    def test_current_food_analysis_uses_saved_formula_without_reasking_name(self):
        state = service._default_state("c1")
        context = {
            "food_submission": None,
            "current_food": {
                "brand": "渴望", "product_name": "原始猎食原味",
                "product_id": 16, "formula_id": 15, "catalog_key": "formula:15",
            },
            "diet_history": [{"brand": "皇家", "product": "肠道处方粮", "formula_id": 304, "is_current": False}],
            "food_list": [], "recent_daily_records": [], "pet_profile": {}, "food_candidates": [],
        }
        analysis = {"product": {"formula_id": 15}, "ingredient_analysis": {"groups": []}}
        with patch.object(service, "_build_context", return_value=context), \
             patch.object(service, "_food_analysis_by_formula", return_value=analysis) as load, \
             patch.object(service, "_generate_answer", return_value=("当前粮分析结果", "test-model")) as generate:
            result = service._handle_context_message(
                "u1", {"pet_id": "p1"}, "分析一下我现在吃的粮", state,
                intent_result=({"primary_intent": "food_analysis", "secondary_intent": None, "slots": {}}, None),
            )
        self.assertEqual(result["response_type"], "result_card")
        self.assertEqual(result["reply"], "当前粮分析结果")
        self.assertEqual(state["slots"]["food_name"], "渴望 原始猎食原味")
        self.assertEqual(state["slots"]["catalog_key"], "formula:15")
        self.assertIsNone(state["current_step"])
        load.assert_called_once_with(15, 15)
        self.assertEqual(generate.call_args.args[2]["food_analysis"], analysis)
        self.assertEqual(generate.call_args.args[2]["food_analysis_scope"], "current")

    def test_history_food_analysis_uses_every_saved_formula_and_keeps_unlinked_records(self):
        state = service._default_state("c1")
        context = {
            "food_submission": None,
            "current_food": {"brand": "渴望", "product_name": "原始猎食原味", "formula_id": 15},
            "diet_history": [
                {"brand": "渴望", "product": "原始猎食原味", "formula_id": 15, "is_current": True},
                {"brand": "皇家", "product": "肠道处方粮", "formula_id": 304, "is_current": False},
                {"brand": "鲜朗", "product": "烘焙全价猫粮", "formula_id": None, "is_current": False},
            ],
            "food_list": [], "recent_daily_records": [], "pet_profile": {}, "food_candidates": [],
        }
        history_analysis = {"product": {"formula_id": 304}, "ingredient_analysis": {"groups": []}}
        with patch.object(service, "_build_context", return_value=context), \
             patch.object(service, "_food_analysis_by_formula", return_value=history_analysis) as load, \
             patch.object(service, "_generate_answer", return_value=("历史粮分析结果", "test-model")) as generate:
            result = service._handle_context_message(
                "u1", {"pet_id": "p1"}, "分析一下我历史吃过的粮", state,
                intent_result=({"primary_intent": "food_analysis", "secondary_intent": None, "slots": {}}, None),
            )
        self.assertEqual(result["response_type"], "result_card")
        self.assertEqual(result["reply"], "历史粮分析结果")
        self.assertEqual(state["slots"]["food_name"], "历史口粮")
        load.assert_called_once_with(304, 15)
        analyses = generate.call_args.args[2]["food_history_analyses"]
        self.assertEqual([item["analysis_status"] for item in analyses], ["available", "unavailable"])
        self.assertEqual(analyses[0]["diet_record"]["formula_id"], 304)
        self.assertIsNone(analyses[1]["diet_record"]["formula_id"])
        self.assertEqual(generate.call_args.args[2]["food_analysis_scope"], "history")

    def test_current_and_history_food_comparison_loads_both_sides(self):
        state = service._default_state("c1")
        context = {
            "food_submission": None,
            "current_food": {
                "brand": "渴望", "product_name": "原始猎食原味",
                "product_id": 16, "formula_id": 15, "catalog_key": "formula:15",
            },
            "diet_history": [
                {"brand": "渴望", "product": "原始猎食原味", "formula_id": 15, "is_current": True},
                {"brand": "皇家", "product": "肠道处方粮", "formula_id": 304, "is_current": False},
            ],
            "food_list": [], "recent_daily_records": [], "pet_profile": {}, "food_candidates": [],
        }
        current_analysis = {"product": {"formula_id": 15}}
        history_analysis = {"product": {"formula_id": 304}, "portrait_comparison": {
            "status": "partial",
            "common": [{"text": "两款粮的益生元支持结构接近（1.36 / 1.26）"}],
            "complements": [],
            "shortcomings": [{"text": "目标粮的脂肪负担比原粮更高"}],
            "missing_dimensions": ["纤维支持"],
            "note": "画像比较描述配方特点，不代表实际健康效果；互补不表示建议混喂。",
        }}
        with patch.object(service, "_build_context", return_value=context), \
             patch.object(service, "_food_analysis_by_formula", side_effect=[current_analysis, history_analysis]) as load, \
             patch.object(service, "_generate_answer", return_value=("对比结果", "test-model")) as generate:
            result = service._handle_context_message(
                "u1", {"pet_id": "p1"}, "对比一下我现在吃的粮和历史粮", state,
                intent_result=({"primary_intent": "food_analysis", "secondary_intent": None, "slots": {}}, None),
            )
        self.assertEqual(result["response_type"], "result_card")
        self.assertIn("渴望 原始猎食原味 vs 皇家 肠道处方粮", result["reply"])
        self.assertIn("共同特点", result["reply"])
        self.assertIn("潜在短板", result["reply"])
        self.assertIn("画像缺项：纤维支持", result["reply"])
        self.assertNotIn("目标粮", result["reply"])
        self.assertNotIn("原粮", result["reply"])
        self.assertEqual(state["slots"]["food_name"], "当前粮与历史口粮")
        self.assertEqual(state["slots"]["catalog_key"], "formula:15")
        self.assertEqual(load.call_args_list[0].args, (15, 15))
        self.assertEqual(load.call_args_list[1].args, (304, 15))
        generate.assert_not_called()
        self.assertEqual(context["food_analysis_scope"], "current_vs_history")
        self.assertEqual(context["food_analysis"]["product"]["formula_id"], 15)
        self.assertEqual(
            context["food_history_analyses"][0]["food_analysis"]["product"]["formula_id"], 304,
        )

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

    def test_soft_stool_text_routes_to_specialized_pipeline(self):
        state = service._default_state("c1")
        self.assertTrue(service._should_route_soft_stool("重重今天软便了", state))
        state["slots"][service.SOFT_STOOL_STATE_SLOT] = {"pet_id": "pet-1"}
        self.assertTrue(service._should_route_soft_stool("没有呕吐", state))
        state["slots"].pop(service.SOFT_STOOL_STATE_SLOT)
        self.assertFalse(service._should_route_soft_stool("今天精神不错", state))

    @patch.object(service, "handle_soft_stool_turn")
    def test_specialized_pipeline_state_is_persisted_between_turns(self, handle_turn):
        handle_turn.return_value = {
            "status": "need_more_info", "reply": "最近有换粮吗？",
            "pattern": "with_baseline_diet", "next_group": "recent_diet_change_group",
            "state": {"pet_id": "pet-1", "turn": 1}, "disclaimer": "仅供参考",
        }
        state = service._default_state("c1")
        reply = service._handle_soft_stool_message(
            "user-1", {"pet_id": "pet-1"}, "软便了", state,
        )
        self.assertEqual(reply["result"]["pipeline"], "soft_stool")
        self.assertEqual(
            state["slots"][service.SOFT_STOOL_STATE_SLOT],
            {"pet_id": "pet-1", "turn": 1, "status": "active"},
        )

    @patch.object(service, "handle_soft_stool_turn")
    def test_legacy_user_level_food_state_is_discarded(self, handle_turn):
        handle_turn.return_value = {
            "status": "need_more_info", "reply": "最近有换粮吗？",
            "pattern": "without_baseline_diet", "next_group": "recent_diet_change_group",
            "state": {"pet_id": "pet-1", "turn": 1}, "disclaimer": "仅供参考",
        }
        state = service._default_state("c1")
        state["slots"][service.SOFT_STOOL_STATE_SLOT] = {
            "pet_id": "pet-1",
            "context": {"baseline_diet": {"source": "food_change_intent.previous_food"}},
        }
        service._handle_soft_stool_message("user-1", {"pet_id": "pet-1"}, "没有换粮", state)
        self.assertIsNone(handle_turn.call_args.args[1]["state"])


if __name__ == "__main__":
    unittest.main()
