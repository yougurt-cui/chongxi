"""Production-feedback regressions, using no paid model or production database."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from services import guardian_workflow as workflow
from services import miniprogram_chat_service as chat
from services import soft_stool_assistant_service as boundary
from soft_stool_assistant import llm, pipeline, policy
import test_soft_stool_consultation as fixtures


class GuardianFeedbackTest(unittest.TestCase):
    def setUp(self):
        for mock in (
            patch.object(llm, "get_chat_model_config", return_value={"api_key": ""}),
            patch.object(chat, "get_chat_model_config", return_value={"api_key": ""}),
            patch.object(pipeline, "build_baseline_diet_profile", return_value=deepcopy(fixtures.BASELINE)),
            patch.object(pipeline, "get_pet_profile", return_value={"name": "测试猫", "diseases": []}),
            patch.object(pipeline, "get_recent_diet_change", return_value={"found": False}),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def state(self):
        state = chat._default_state("conversation-1")
        nested = pipeline.new_state("pet-1")
        nested["context"].update(baseline_loaded=True, baseline_diet=deepcopy(fixtures.BASELINE),
                                 active_question_group="history_food_group", active_question_fields=["history_foods_reviewed"])
        nested["slots"]["history_foods_reviewed"] = "以前吃过旧粮，便便正常"
        nested["slots"]["soft_stool_days"] = 2
        nested["slot_status"]["soft_stool_days"] = {"status": "confirmed", "source_text": "软便两天", "turn": 1}
        state["slots"][chat.SOFT_STOOL_STATE_SLOT] = nested
        state["primary_intent"] = "symptom_consult"
        return state

    def recognize_model(self, text, state, result):
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", return_value=json.dumps(result, ensure_ascii=False)) as model, \
             patch.object(chat, "_extract_intent", side_effect=AssertionError("no extra routing call")):
            decision = chat._recognize_guardian_turn(text, state)
        self.assertEqual(model.call_count, 1)
        return decision

    def test_missing_duration_and_normal_status_cannot_be_fabricated(self):
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", return_value=json.dumps({
                 "soft_stool_days": 0, "energy": "normal", "appetite": "normal",
                 "field_evidence": {"soft_stool_days": "软便了", "energy": "软便了", "appetite": "软便了"},
             })):
            extracted = llm.extract_user_info("软便了", pipeline.new_state("pet-1"))
        for field in ("soft_stool_days", "energy", "appetite"):
            self.assertNotIn(field, extracted)

    def test_numeric_model_value_must_match_its_evidence(self):
        values, _ = llm._ground_model_values({"soft_stool_days": 20,
            "field_evidence": {"soft_stool_days": "软便两天"}}, "软便两天", {})
        self.assertNotIn("soft_stool_days", values)

    def test_explicit_today_is_preserved_without_zero_day_wording(self):
        result = pipeline.run_soft_stool_turn("pet-1", "今天开始软便")
        self.assertEqual(result["state"]["slots"]["soft_stool_days"], 0)
        self.assertIn("今天开始", result["reply"])
        self.assertNotIn("0 天", result["reply"])

    def test_three_turn_screenshot_path_has_no_symptom_questionnaire(self):
        result = pipeline.run_soft_stool_turn("pet-1", "软便了")
        for text in ("没有", "没有"):
            result = pipeline.run_soft_stool_turn("pet-1", text, result["state"])
        self.assertEqual(result["status"], "answered")
        self.assertIsNone(result["state"]["slots"]["soft_stool_days"])
        self.assertIsNone(result["state"]["slots"]["recent_diet_change"])
        self.assertIsNone(result["state"]["slots"]["vomiting"])
        self.assertIn("下一步：", result["reply"])

    def test_nonanswers_advance_once_then_give_advice(self):
        result = pipeline.run_soft_stool_turn("pet-1", "软便了")
        result = pipeline.run_soft_stool_turn("pet-1", "嗯", result["state"])
        self.assertEqual(result["next_group"], "medical_history_group")
        result = pipeline.run_soft_stool_turn("pet-1", "嗯", result["state"])
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["limited_info"])

    def test_later_evidence_only_contains_new_information(self):
        result = pipeline.run_soft_stool_turn("pet-1", "软便了")
        result = pipeline.run_soft_stool_turn("pet-1", "以前吃过旧粮，便便正常", result["state"])
        self.assertIn("历史饮食", result["reply_parts"][0]["reply"])
        self.assertNotIn("0.418", result["reply"])
        self.assertNotIn("碳水结构压力", result["reply"])

    def test_bare_history_food_name_is_preserved_in_screenshot_dialogue(self):
        result = pipeline.run_soft_stool_turn("pet-1", "软便了")
        self.assertEqual(result["next_group"], "history_food_group")
        result = pipeline.run_soft_stool_turn("pet-1", "蓝氏猎鸟乳鸽", result["state"])
        nested = result["state"]
        self.assertEqual(nested["slots"]["history_foods_reviewed"], "蓝氏猎鸟乳鸽")
        self.assertEqual(nested["slot_status"]["history_foods_reviewed"]["status"], "confirmed")
        self.assertEqual(nested["slot_status"]["history_foods_reviewed"]["source_text"], "蓝氏猎鸟乳鸽")
        self.assertIn("历史饮食补充：蓝氏猎鸟乳鸽", result["reply_parts"][0]["reply"])
        self.assertNotIn("暂时没有补充资料", result["reply_parts"][0]["reply"])
        self.assertIsNone(nested["slots"]["new_product"])
        self.assertEqual(result["next_group"], "medical_history_group")
        result = pipeline.run_soft_stool_turn("pet-1", "没有相关病例", nested)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["actions"][0]["target_flow"], "food_switch")
        state = chat._default_state("c1")
        state["slots"][chat.SOFT_STOOL_STATE_SLOT] = result["state"]
        decision, _ = chat._recognize_guardian_turn("帮我推荐一款主粮", state)
        self.assertEqual(decision["_target_flow"], "food_switch")
        chat._prepare_food_handoff(state, "pet-1")
        packet = state["slots"][chat.GUARDIAN_CONTEXT_SLOT]
        self.assertEqual(packet["facts"]["history_foods_reviewed"], "蓝氏猎鸟乳鸽")
        self.assertEqual(state["slots"]["current_food"], "皇家 BK34")

    def test_history_name_answers_keep_spelling_and_explicit_performance(self):
        state = pipeline.new_state("pet-1")
        state["context"].update(active_question_group="history_food_group",
                                 active_question_fields=["history_foods_reviewed"])
        for text in ("蓝氏 猎鸟乳鸽", "蓝氏猎鸟乳鸽，便便正常", "皇家、蓝氏猎鸟乳鸽", "A粮"):
            with self.subTest(text=text):
                extracted = llm.extract_user_info(text, state)
                self.assertEqual(extracted["history_foods_reviewed"], text)
                self.assertNotIn("current_product", extracted)
                self.assertNotIn("new_product", extracted)

    def test_bare_name_is_grounded_even_when_model_omits_the_answer(self):
        state = pipeline.new_state("pet-1")
        state["context"].update(active_question_group="history_food_group",
                                 active_question_fields=["history_foods_reviewed"])
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", return_value="{}"):
            extracted = llm.extract_user_info("蓝氏猎鸟乳鸽", state)
        self.assertEqual(extracted["history_foods_reviewed"], "蓝氏猎鸟乳鸽")
        self.assertEqual(extracted["_field_evidence"]["history_foods_reviewed"], "蓝氏猎鸟乳鸽")

    def test_history_question_does_not_turn_acknowledgements_or_requests_into_food(self):
        state = pipeline.new_state("pet-1")
        state["context"].update(active_question_group="history_food_group",
                                 active_question_fields=["history_foods_reviewed"])
        for text in ("嗯", "好的", "知道了", "谢谢", "那啥", "跳过", "不知道", "先不聊了", "帮我推荐一款", "想换蓝氏猎鸟乳鸽", "蓝氏是什么牌子？"):
            with self.subTest(text=text):
                self.assertNotIn("history_foods_reviewed", llm.extract_user_info(text, state))
        state["context"].update(active_question_group="medical_history_group",
                                 active_question_fields=["disease_history_reviewed"])
        self.assertNotIn("history_foods_reviewed", llm.extract_user_info("蓝氏猎鸟乳鸽", state))

    def test_model_reasoning_replaces_rather_than_appends_template(self):
        state = pipeline.new_state("pet-1")
        state["analysis"].update(show_product_evidence=True, assessment_plan=pipeline.build_assessment_plan(state))
        state["context"]["baseline_diet"] = deepcopy(fixtures.BASELINE)
        reply = "碳水结构压力、蛋白消化压力值得对照以前吃粮的表现，配方数据只作参考。"
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture", "model": "fixture-model"}), \
             patch.object(llm, "call_real_llm", return_value=json.dumps({"reasoning_message": reply})):
            parts = llm.generate_two_stage_response(state, "事实", "事实", "以前吃过哪些粮？")
        self.assertEqual(parts[1]["reply"], reply + "\n\n以前吃过哪些粮？")
        self.assertEqual(state["analysis"]["generation_source"], "model")

    def test_response_failure_has_sanitized_diagnostic(self):
        state = pipeline.new_state("pet-1")
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", side_effect=TimeoutError("secret-token")), \
             self.assertLogs(llm.logger, level="WARNING") as logs:
            llm.generate_two_stage_response(state, "事实", "事实", "以前吃过哪些粮？")
        self.assertEqual(state["analysis"]["fallback_reason"], "TimeoutError")
        self.assertNotIn("secret-token", str(logs.output))

    def test_short_answers_do_not_call_slot_model(self):
        state = self.state()["slots"][chat.SOFT_STOOL_STATE_SLOT]
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", side_effect=AssertionError("no model for short answer")) as model:
            extracted = llm.extract_user_info("没有", state)
        self.assertEqual(extracted["history_foods_reviewed"], "没有其他主粮记录")
        self.assertNotIn("recent_diet_change", extracted)
        model.assert_not_called()

    def test_explicit_food_request_interrupts_pending_soft_stool_offline(self):
        with patch.object(llm, "call_real_llm", side_effect=AssertionError("no slot model")), \
             patch.object(chat, "_extract_intent", side_effect=AssertionError("no routing model")):
            decision, _ = chat._recognize_guardian_turn("帮我推荐一款主粮", self.state())
        selected = workflow.select_strategy("帮我推荐一款主粮", {"_turn_decision": decision}, chat.GUARDIAN_STRATEGIES)
        self.assertEqual(selected.name, "food_selection")

    def test_semantic_food_switch_reuses_the_slot_model(self):
        text = "有没有更适合它的选择呀"
        decision, _ = self.recognize_model(text, self.state(), {"dialogue": {
            "action": "switch", "target_flow": "food_switch", "confidence": .95, "source_text": text,
        }})
        self.assertEqual(decision["_target_flow"], "food_switch")

    def test_dispatch_does_not_extract_a_second_time(self):
        state = self.state()
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture", "model": "fixture-model"}), \
             patch.object(llm, "call_real_llm", side_effect=["{}", json.dumps({"reasoning_message": "先结合历史饮食与健康背景，整理下一步。"})]) as model, \
             patch.object(boundary, "get_cat_profile", return_value={"id": "pet-1"}):
            recognized = chat._recognize_guardian_turn("软便还是没什么变化", state)
            chat._handle_soft_stool_message("user-1", {"pet_id": "pet-1"}, "软便还是没什么变化", state, intent_result=recognized)
        self.assertEqual(model.call_count, 2, "one combined extraction plus one generation, no extra routing call")

    def test_semantic_other_module_is_recognized_in_mid_conversation(self):
        text = "想弄明白这个配方里的原料有什么用"
        decision, _ = self.recognize_model(text, self.state(), {"dialogue": {
            "action": "switch", "target_flow": "general", "target_intent": "ingredient_analysis",
            "confidence": .95, "source_text": text,
        }})
        self.assertEqual(decision["_target_flow"], "general")
        self.assertEqual(decision["primary_intent"], "ingredient_analysis")

    def test_unclear_input_does_not_force_handoff(self):
        text = "那啥"
        decision, _ = self.recognize_model(text, self.state(), {"dialogue": {
            "action": "switch", "target_flow": "food_switch", "confidence": .3, "source_text": text,
        }})
        self.assertEqual(decision["_target_flow"], "soft_stool")

    def test_switch_snapshot_preserves_facts_sources_and_original_flow(self):
        state = self.state()
        chat._prepare_food_handoff(state, "pet-1")
        packet = state["slots"][chat.GUARDIAN_CONTEXT_SLOT]
        self.assertEqual(packet["facts"]["soft_stool_days"], 2)
        self.assertEqual(packet["fact_sources"]["soft_stool_days"]["source_text"], "软便两天")
        self.assertEqual(state["slots"]["current_food"], "皇家 BK34")
        self.assertEqual(state["slots"]["switch_reason"], "digestive")
        self.assertEqual(state["slots"][chat.SOFT_STOOL_STATE_SLOT]["status"], "paused")
        packet["facts"]["soft_stool_days"] = 99
        self.assertEqual(state["slots"][chat.SOFT_STOOL_STATE_SLOT]["slots"]["soft_stool_days"], 2)

    def test_new_fact_on_handoff_has_its_source_message(self):
        state = self.state()
        chat._prepare_food_handoff(state, "pet-1", {"soft_stool_days": 3,
            "_field_evidence": {"soft_stool_days": "软便三天"}}, "message-new")
        packet = state["slots"][chat.GUARDIAN_CONTEXT_SLOT]
        self.assertEqual(packet["facts"]["soft_stool_days"], 3)
        self.assertEqual(packet["fact_sources"]["soft_stool_days"]["message_id"], "message-new")

    def test_a_pure_switch_preserves_the_source_flow_turn(self):
        state = self.state()
        before = state["slots"][chat.SOFT_STOOL_STATE_SLOT]["turn"]
        chat._prepare_food_handoff(state, "pet-1", {"unknown_fields": [], "_field_evidence": {},
                                                 "_dialogue": {"action": "switch", "target_flow": "food_switch"}})
        self.assertEqual(state["slots"][chat.SOFT_STOOL_STATE_SLOT]["turn"], before)

    def test_food_preference_with_symptom_word_does_not_restart_soft_stool(self):
        state = self.state()
        chat._prepare_food_handoff(state, "pet-1")
        state["current_step"] = "target_requirement"
        with patch.object(chat, "_extract_intent", side_effect=AssertionError("no routing model")):
            decision, _ = chat._recognize_guardian_turn("希望改善软便，价格适中", state)
        self.assertEqual(decision["_target_flow"], "food_switch")
        self.assertEqual(decision["slots"]["target_requirement"], "希望改善软便，价格适中")

    def test_pause_is_idempotent_for_food_flow_followup_count(self):
        state = self.state()
        chat._prepare_food_handoff(state, "pet-1")
        state["current_step"] = "target_requirement"
        state["followup_count"] = 1
        chat._pause_soft_stool(state)
        self.assertEqual(state["primary_intent"], "food_switch")
        self.assertEqual(state["followup_count"], 1)

    def test_resuming_keeps_the_unanswered_background_step(self):
        result = pipeline.run_soft_stool_turn("pet-1", "软便了")
        state = chat._default_state("c1")
        state["slots"][chat.SOFT_STOOL_STATE_SLOT] = result["state"]
        chat._prepare_food_handoff(state, "pet-1")
        with patch.object(boundary, "get_cat_profile", return_value={"id": "pet-1"}):
            reply = chat._handle_soft_stool_message("user-1", {"pet_id": "pet-1"}, "继续聊软便", state)
        self.assertEqual(reply["result"]["next_group"], "history_food_group")
        self.assertIsNone(state["slots"][chat.SOFT_STOOL_STATE_SLOT]["slots"]["history_foods_reviewed"])

    def test_existing_explicit_pause_does_not_become_a_symptom_answer(self):
        state = self.state()
        decision = chat._recognize_guardian_turn("先不聊软便了", state)
        result = chat._handle_context_message("user-1", {"pet_id": "pet-1"}, "先不聊软便了", state, intent_result=decision)
        self.assertEqual(state["slots"][chat.SOFT_STOOL_STATE_SLOT]["status"], "paused")
        self.assertNotIn("症状", result["reply"])
        self.assertIsNone(state["slots"][chat.SOFT_STOOL_STATE_SLOT]["slots"]["vomiting"])

    def test_old_fabricated_zero_is_removed_on_resume_and_handoff(self):
        state = self.state()
        nested = state["slots"][chat.SOFT_STOOL_STATE_SLOT]
        nested["rule_version"] = "soft-stool-consultation-v3"
        nested["slots"]["soft_stool_days"] = 0
        nested["slot_status"]["soft_stool_days"] = {"status": "confirmed", "turn": 1}
        nested["summary"] = {"facts": "软便持续约0天"}
        packet = chat._share_guardian_context(state, "pet-1", "food_switch")
        self.assertIsNone(packet["facts"]["soft_stool_days"])
        self.assertEqual(packet["rule_version"], policy.RULE_VERSION)
        self.assertNotIn("0天", str(packet["summary"]))

    def test_limited_hint_only_shown_once_on_final_reply(self):
        result = pipeline.run_soft_stool_turn("pet-1", "软便了")
        for text in ("没有", "没有"):
            with patch.object(chat, "handle_soft_stool_turn", return_value=result):
                reply = chat._handle_soft_stool_message("user-1", {"pet_id": "pet-1"}, text, chat._default_state("c1"))
            self.assertFalse(any(item["result"]["limited_info"] for item in reply["messages"]))
            result = pipeline.run_soft_stool_turn("pet-1", text, result["state"])
        with patch.object(chat, "handle_soft_stool_turn", return_value=result):
            reply = chat._handle_soft_stool_message("user-1", {"pet_id": "pet-1"}, "没有", chat._default_state("c1"))
        self.assertEqual([item["result"]["limited_info"] for item in reply["messages"]], [False, True])

    def test_new_strategy_and_step_can_be_registered_without_dispatch_edits(self):
        custom = workflow.FlowStrategy("new_case", lambda text, state: state["flow"] == "new_case", lambda *args: {"ok": True})
        selected = workflow.select_strategy("一个新案例", {"flow": "new_case"}, (custom, chat.GENERAL_STRATEGY))
        self.assertEqual(selected.handle(), {"ok": True})
        steps = (workflow.Step("only_when_changed", ("new_product",), lambda state, fields: "新粮是什么？",
                               lambda state: state["slots"].get("recent_diet_change") is True),)
        state = pipeline.new_state("pet-1")
        self.assertEqual(workflow.next_step(state, steps, policy.unanswered), (None, None, []))
        state["slots"]["recent_diet_change"] = True
        self.assertEqual(workflow.next_step(state, steps, policy.unanswered)[0], "only_when_changed")


if __name__ == "__main__":
    unittest.main()
