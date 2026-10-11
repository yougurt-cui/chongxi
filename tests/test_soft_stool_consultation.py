"""Offline acceptance scenarios: use fixtures, never a production DB or model."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import pymysql

from soft_stool_assistant import llm, pipeline, policy, tools
from services import miniprogram_chat_service as chat

PRODUCT = {
    "name": "BK34", "display_name": "皇家 BK34", "catalog_key": "bk34",
    "protein_sources": ["鸭", "鸡", "牛"], "fat_sources": ["大豆油", "鸡油"],
    "gut_friendly_score": 34.9, "soft_stool_risk": {"risk_level": "高风险", "risk_index": .418},
    "soft_stool_mechanisms": {
        "confidence": "limited", "reference_pool_size": 13, "reference_pool_version": "fixture",
        "mechanisms": [
            {"mechanism": "碳水结构压力", "role": "primary", "contribution": .24},
            {"mechanism": "蛋白消化压力", "role": "primary", "contribution": .22},
            {"mechanism": "脂肪消化负担", "role": "secondary", "contribution": .1385},
        ], "amplifiers": [{"mechanism": "便便成形支持偏弱", "percentile": .2308}],
    },
}
BASELINE = {"has_diet_data": True, "products": [PRODUCT], "detailed_products": [PRODUCT],
            "nutrition_summary": None, "source": "cat_profile.food_product", "notes": []}


class ConsultationTest(unittest.TestCase):
    def setUp(self):
        patches = [
            patch.object(llm, "get_chat_model_config", return_value={"api_key": ""}),
            patch.object(pipeline, "build_baseline_diet_profile", return_value=deepcopy(BASELINE)),
            patch.object(pipeline, "get_pet_profile", return_value={"name": "重重", "diseases": []}),
            patch.object(pipeline, "get_recent_diet_change", return_value={"found": False}),
            patch.object(pipeline, "get_product_by_name", return_value=deepcopy(PRODUCT)),
            patch.object(pipeline, "build_manual_old_new_compare", return_value={"compare": {}}),
        ]
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def turn(self, text, state=None):
        result = pipeline.run_soft_stool_turn("pet-1", text, state)
        self.assertEqual(len(result["reply_parts"]), 2)
        self.assertEqual(result["reply_parts"][0]["response_type"], "evidence_card")
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result

    def test_no_switch_but_canned_addition_and_separate_mucus(self):
        result = self.turn("没换主粮，但昨天加了罐头，占一半，软便两天，没有呕吐没有血，有黏液，食欲和精神都正常")
        slots = result["state"]["slots"]
        self.assertFalse(slots["main_food_changed"])
        self.assertTrue(slots["recent_diet_change"])
        self.assertTrue(slots["wet_food_added"])
        self.assertFalse(slots["blood_in_stool"])
        self.assertTrue(slots["mucus_in_stool"])
        self.assertIsNone(result["state"]["analysis"]["diet_compare"])
        self.assertNotEqual(result["reply_parts"][1]["response_type"], "risk_alert")

    def test_grouped_dialogue_reaches_actionable_conclusion(self):
        result = self.turn("软便了")
        self.assertEqual(result["next_group"], "recent_diet_change_group")
        self.assertNotIn("主要关注", result["reply_parts"][0]["reply"])
        self.assertIn("碳水结构压力、蛋白消化压力", result["reply_parts"][1]["reply"])
        self.assertIn("数据有限", result["reply_parts"][1]["reply"])
        result = self.turn("没有", result["state"])
        self.assertEqual(result["next_group"], "symptom_group")
        result = self.turn("软便两天，没有呕吐没有血没有黏液", result["state"])
        self.assertEqual(result["next_group"], "risk_group")
        result = self.turn("都正常", result["state"])
        self.assertEqual(result["next_group"], "history_and_disease_group")
        self.assertEqual(result["actions"][0]["type"], "medical_record")
        result = self.turn("都没有", result["state"])
        self.assertEqual(result["status"], "answered")
        for label in ("下一步：", "观察：", "复评：", "不能优先归因到换粮"):
            self.assertIn(label, result["reply"])
        self.assertEqual(result["state"]["status"], "answered")

    def test_unknowns_are_not_negative_or_repeated(self):
        result = self.turn("软便了")
        result = self.turn("不知道", result["state"])
        self.assertEqual(result["next_group"], "symptom_group")
        self.assertIsNone(result["state"]["slots"]["recent_diet_change"])
        for _ in range(3):
            result = self.turn("不知道", result["state"])
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["limited_info"])
        self.assertIn("有限信息", result["reply"])
        self.assertNotIn("没有明确近期饮食变化", result["reply"])

    def test_bare_duration_uses_previous_group(self):
        result = self.turn("没有饮食变化，软便了，没有呕吐没有血没有黏液，食欲精神都正常")
        self.assertEqual(result["next_group"], "symptom_group")
        result = self.turn("两天", result["state"])
        self.assertEqual(result["state"]["slots"]["soft_stool_days"], 2)
        self.assertIsNone(result["state"]["slots"]["change_days"])

    def test_partial_history_answer_does_not_complete_diseases(self):
        state = pipeline.new_state("pet-1")
        state["context"].update(active_question_group="history_and_disease_group",
                                active_question_fields=["history_foods_reviewed", "disease_history_reviewed"])
        result = llm.extract_user_info("以前吃过鸡肉粮，没有其他主粮", state)
        self.assertIn("history_foods_reviewed", result)
        self.assertNotIn("disease_history_reviewed", result)
        result = llm.extract_user_info("没有其他疾病", state)
        self.assertIn("disease_history_reviewed", result)
        self.assertNotIn("history_foods_reviewed", result)

    def test_danger_does_not_touch_products_or_model(self):
        with patch.object(pipeline, "build_baseline_diet_profile", side_effect=AssertionError("must not query")), \
             patch.object(llm, "call_real_llm", side_effect=AssertionError("must not call")):
            result = self.turn("软便有血，没精神")
        self.assertEqual(result["reply_parts"][1]["response_type"], "risk_alert")
        self.assertIsNone(result["next_group"])
        self.assertIn("宠物医院", result["reply"])

    def test_missing_tables_do_not_block_followup(self):
        with patch.object(pipeline, "build_baseline_diet_profile", side_effect=pymysql.OperationalError(1146, "table absent")):
            result = self.turn("软便了")
        self.assertEqual(result["next_group"], "recent_diet_change_group")
        self.assertIn("暂不可用", result["reply"])

    def test_intention_date_is_not_an_actual_food_change(self):
        with patch.object(pipeline, "get_recent_diet_change", return_value={
            "found": True, "old_product": "旧粮", "new_product": "BK34", "change_days": 3,
        }):
            result = self.turn("软便了")
        self.assertIsNone(result["state"]["slots"]["recent_diet_change"])
        self.assertIsNone(result["state"]["slots"]["change_days"])
        self.assertEqual(result["next_group"], "recent_diet_change_group")

    def test_correction_removes_stale_comparison(self):
        result = self.turn("原来吃旧粮，现在换成BK34，三天前开始，一半，软便两天")
        result["state"]["analysis"]["diet_compare"] = {"delta": {"protein": 5}}
        result = self.turn("说错了，没有饮食变化", result["state"])
        self.assertFalse(result["state"]["slots"]["recent_diet_change"])
        self.assertIsNone(result["state"]["slots"]["new_product"])
        self.assertIsNone(result["state"]["analysis"]["diet_compare"])

    def test_change_after_symptoms_is_counter_evidence(self):
        result = self.turn("昨天换成BK34，一半，软便已经五天，没有呕吐没有血没有黏液，食欲和精神都正常")
        self.assertIn("不能用这次变化解释软便的起点", result["reply"])

    def test_repeated_non_answers_eventually_finish(self):
        result = self.turn("软便了")
        for _ in range(12):
            result = self.turn("嗯", result["state"])
            if result["status"] == "answered":
                break
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["limited_info"])

    def test_uncertain_and_historical_symptoms_are_not_current_danger(self):
        for text in ("不知道有没有呕吐", "以前吐过", "如果便血该怎么办", "病历里有血常规"):
            with self.subTest(text=text):
                extracted = llm.extract_user_info(text, pipeline.new_state("pet-1"))
                self.assertFalse(policy.urgent_risk(extracted))
        self.assertIn("vomiting", llm.extract_user_info("不知道有没有呕吐", {})["unknown_fields"])
        self.assertTrue(llm.extract_user_info("没有便血，但现在便血了", {})["blood_in_stool"])

    def test_latest_health_statement_wins_over_historical_normal_status(self):
        slots = llm.extract_user_info("以前食欲精神都正常，但今天食欲下降，精神变差", {})
        self.assertEqual(slots["appetite"], "poor")
        self.assertEqual(slots["energy"], "poor")
        self.assertTrue(policy.urgent_risk(slots))
        slots = llm.extract_user_info("昨天没精神，但现在精神正常", {})
        self.assertEqual(slots["energy"], "normal")


    def test_food_and_symptom_dates_are_not_confused(self):
        slots = llm.extract_user_info("三天前换成BK34，昨天开始软便", {})
        self.assertEqual(slots["change_days"], 3)
        self.assertEqual(slots["soft_stool_days"], 1)
        slots = llm.extract_user_info("三天前软便，昨天换成BK34", {})
        self.assertEqual(slots["change_days"], 1)
        self.assertEqual(slots["soft_stool_days"], 3)

    def test_current_food_correction_refreshes_baseline_without_becoming_old_food(self):
        result = self.turn("一直吃皇家 BK34，没有饮食变化")
        self.assertEqual(result["state"]["slots"]["current_product"], "皇家 BK34")
        self.assertIsNone(result["state"]["slots"]["old_product"])
        with patch.object(pipeline, "build_baseline_diet_profile", return_value=deepcopy(BASELINE)) as load:
            result = self.turn("说错了，现在吃另一款粮", result["state"])
        self.assertEqual(load.call_args.kwargs["override"]["products"][0]["ref"], "另一款粮")
        self.assertIsNone(result["state"]["slots"]["old_product"])

    def test_simultaneous_food_events_are_preserved(self):
        result = self.turn("三天前换成BK34，还加了罐头和零食")
        slots = result["state"]["slots"]
        self.assertTrue(slots["main_food_changed"])
        self.assertTrue(slots["wet_food_added"])
        self.assertTrue(slots["snack_added"])
        self.assertEqual(slots["change_type"], "multiple")


    def test_bare_food_name_uses_the_asked_product_field(self):
        state = pipeline.new_state("pet-1")
        state["context"].update(active_question_group="recent_diet_change_group",
                                active_question_fields=["current_product"])
        self.assertEqual(llm.extract_user_info("皇家 BK34", state)["current_product"], "皇家 BK34")
        self.assertNotIn("current_product", llm.extract_user_info("嗯", state))


    def test_planned_food_switch_is_not_an_actual_event(self):
        slots = llm.extract_user_info("软便两天，打算换成BK34", {})
        self.assertEqual(slots["soft_stool_days"], 2)
        self.assertNotIn("recent_diet_change", slots)
        self.assertNotIn("new_product", slots)
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", return_value='{"recent_diet_change":true,"new_product":"BK34","change_ratio":0.5}'):
            slots = llm.extract_user_info("准备换成BK34，占一半", {})
        self.assertNotIn("recent_diet_change", slots)
        self.assertNotIn("new_product", slots)
        self.assertNotIn("change_ratio", slots)


    def test_model_extraction_validation_and_failure_fallback(self):
        state = pipeline.new_state("pet-1")
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", return_value=json.dumps({
                 "recent_diet_change": True, "change_ratio": 50, "change_days": True,
                 "mucus_in_stool": "false", "energy": "normal", "unexpected": "discard",
             })):
            result = llm.extract_user_info("新加了些食物", state)
        self.assertTrue(result["recent_diet_change"])
        for key in ("change_ratio", "change_days", "mucus_in_stool", "unexpected"):
            self.assertNotIn(key, result)
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", side_effect=TimeoutError):
            self.assertTrue(llm.extract_user_info("加了罐头", state)["wet_food_added"])

    def test_explicit_unknown_beats_a_model_guess(self):
        state = pipeline.new_state("pet-1")
        state["context"].update(active_question_group="recent_diet_change_group",
                                active_question_fields=["recent_diet_change"])
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", return_value='{"recent_diet_change":false}'):
            extracted = llm.extract_user_info("不知道", state)
        self.assertNotIn("recent_diet_change", extracted)
        pipeline.update_slots(state, extracted)
        self.assertIsNone(state["slots"]["recent_diet_change"])
        self.assertEqual(state["slot_status"]["recent_diet_change"]["status"], "unknown")


    def test_model_cannot_rewrite_facts_questions_or_add_numeric_regimen(self):
        state = pipeline.new_state("pet-1")
        state["context"]["baseline_diet"] = deepcopy(BASELINE)
        state["analysis"]["assessment_plan"] = pipeline.build_assessment_plan(state)
        model = {"evidence_message": "肠胃友好99分", "reasoning_message": "肯定是BK34引起的",
                 "action_steps": ["新粮占90%，观察7天"], "observation": "禁食", "review": "不用就医"}
        with patch.object(llm, "get_chat_model_config", return_value={"api_key": "fixture"}), \
             patch.object(llm, "call_real_llm", return_value=json.dumps(model, ensure_ascii=False)):
            parts = llm.generate_two_stage_response(state, "肠胃友好评分34.9", "已有事实", None)
            question_parts = llm.generate_two_stage_response(state, "已有事实", "已有事实", "有没有便血？")
        self.assertEqual(parts[0]["reply"], "肠胃友好评分34.9")
        for text in ("99", "90%", "7天", "肯定是", "禁食"):
            self.assertNotIn(text, parts[1]["reply"])
        self.assertTrue(question_parts[1]["reply"].endswith("有没有便血？"))


class NutritionComparisonTest(unittest.TestCase):
    def test_missing_and_incompatible_values_are_unknown(self):
        self.assertIsNone(tools._delta(None, 30))
        before = {"has_diet_data": True, "nutrition_summary": {"protein": 30,
            "metric_specs": {"protein": {"unit": "%", "basis": "干物质", "operator": ">="}}}}
        after = {"name": "新粮", "protein": 35, "metric_specs": {
            "protein": {"unit": "%", "basis": "原物质", "operator": ">="}}}
        self.assertIsNone(tools.compare_diets(before, after)["delta"]["protein"])
        after["metric_specs"]["protein"]["basis"] = "干物质"
        self.assertEqual(tools.compare_diets(before, after)["delta"]["protein"], 5)
        self.assertIsNone(tools.compare_diets(before, after)["delta"]["fat"])

    def test_ambiguous_brand_does_not_pick_an_arbitrary_food(self):
        with patch.object(tools, "query_one", return_value=None), patch.object(tools, "query_all", return_value=[
            {"product_key": "皇家||BK34"}, {"product_key": "皇家||另一款"},
        ]), patch.object(tools, "_build_product") as build:
            self.assertIsNone(tools.get_product_by_name("皇家"))
        build.assert_not_called()


    def test_incomplete_mixed_food_is_not_renormalized(self):
        complete = {"name": "完整粮", "catalog_key": "a", "product_key": "a", "ratio": .5,
                    "protein": 30, "fat": 15, "fiber": 3, "nutrition_basis": "原物质",
                    "metric_specs": {k: {"unit": "%"} for k in ("protein", "fat", "fiber")}}
        missing = {"name": "缺失粮", "catalog_key": "b", "product_key": "b", "ratio": .5}
        with patch.object(tools, "get_baseline_diet", return_value={"has_diet_data": True,
            "products": [complete, missing], "diet_mode": "mixed"}):
            profile = tools.build_baseline_diet_profile("pet-1")
        self.assertIsNone(profile["nutrition_summary"])
        self.assertEqual(len(profile["detailed_products"]), 2)


    def test_ambiguous_guarantee_basis_is_not_last_row_wins(self):
        with patch.object(tools, "query_all", return_value=[
            {"metric_name": "粗蛋白", "metric_value": 30, "metric_unit": "%", "basis": "原物质", "operator_symbol": ">="},
            {"metric_name": "粗蛋白", "metric_value": 35, "metric_unit": "%", "basis": "干物质", "operator_symbol": ">="},
        ]):
            self.assertIsNone(tools._nutrition_by_formula(1)["protein"])


class RoutingTest(unittest.TestCase):
    def test_pause_resume_and_completed_followup(self):
        state = chat._default_state("c1")
        nested = pipeline.new_state("pet-1")
        state["slots"][chat.SOFT_STOOL_STATE_SLOT] = nested
        self.assertFalse(chat._should_route_soft_stool("先不聊软便了，帮我看看玩具", state))
        self.assertFalse(chat._should_route_soft_stool("帮我看看玩具", state))
        self.assertTrue(chat._should_route_soft_stool("先不聊软便，但现在有便血", state))
        chat._pause_soft_stool(state)
        self.assertFalse(chat._should_route_soft_stool("没有", state))
        self.assertTrue(chat._should_route_soft_stool("继续聊软便", state))
        nested["status"] = "answered"
        self.assertTrue(chat._should_route_soft_stool("那接下来怎么办", state))
        self.assertFalse(chat._should_route_soft_stool("猫喜欢什么玩具", state))

    def test_recurrence_only_resets_a_completed_episode(self):
        for status in ("active", "answered"):
            with self.subTest(status=status):
                state = chat._default_state("c1")
                previous = pipeline.new_state("pet-1")
                previous["status"] = status
                state["slots"][chat.SOFT_STOOL_STATE_SLOT] = previous
                result = {"status": "need_more_info", "state": pipeline.new_state("pet-1"), "reply": "请补充"}
                with patch.object(chat, "handle_soft_stool_turn", return_value=result) as handle:
                    chat._handle_soft_stool_message("user-1", {"pet_id": "pet-1"}, "今天又软便了", state)
                supplied = handle.call_args.args[1]["state"]
                self.assertIsNone(supplied) if status == "answered" else self.assertIs(supplied, previous)


    def test_completed_summary_and_two_messages_are_retained(self):
        nested = pipeline.new_state("pet-1")
        result = {"status": "answered", "state": nested, "reply": "事实\\n建议", "reply_parts": [
            {"response_type": "evidence_card", "reply": "事实"},
            {"response_type": "result_card", "reply": "建议"}], "rule_version": policy.RULE_VERSION}
        state = chat._default_state("c1")
        with patch.object(chat, "handle_soft_stool_turn", return_value=result):
            reply = chat._handle_soft_stool_message("user-1", {"pet_id": "pet-1"}, "软便", state)
        self.assertEqual(state["slots"][chat.SOFT_STOOL_STATE_SLOT]["status"], "answered")
        self.assertEqual([x["result"]["sequence_no"] for x in reply["messages"]], [1, 2])
        self.assertEqual(reply["result"]["rule_version"], policy.RULE_VERSION)


if __name__ == "__main__":
    unittest.main()
