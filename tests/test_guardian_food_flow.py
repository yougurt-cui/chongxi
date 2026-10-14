from copy import deepcopy
import unittest
from unittest.mock import patch
from services import guardian_food_flow_service as food
from services import miniprogram_chat_service as chat

OLD = dict(catalog_key="formula:1", formula_id=1, product_id=11, brand="原牌", label="原牌原粮", product_name="原粮",
           ingredient_composition="鸡肉，米", life_stage="adult", product_type="干粮")
NEW = dict(catalog_key="formula:2", formula_id=2, product_id=22, brand="蓝氏", label="蓝氏猎鸟乳鸽", product_name="猎鸟乳鸽",
           ingredient_composition="鸽肉，鸭肉", life_stage="adult", product_type="干粮")
V2 = {**NEW, "catalog_key": "formula:3", "formula_id": 3, "label": "蓝氏猎鸟乳鸽2.0", "product_name": "猎鸟乳鸽2.0"}
INCOMPLETE = {**NEW, "catalog_key": "formula:4", "formula_id": 4, "product_id": 44,
              "label": "渴望室内伴侣", "product_name": "室内伴侣", "product_type": None, "life_stage": None}


class FoodFlowTests(unittest.TestCase):
    def setUp(self):
        patch.object(food, 'load_product_portraits', return_value={}).start()
        patch.object(food, 'load_nutrition', return_value={}).start()
        patch.object(food, 'load_risks', return_value={}).start()
        self.state = chat._default_state("c")
        self.state["primary_intent"] = "food_switch"
        self.context = {"pet_profile": {"age_months": None, "food_formula_id": 1},
                        "current_food": {"brand": "原牌", "product_name": "原粮"}}
        mock = patch.object(food, "list_standardized_product_options", return_value={"items": [OLD, NEW, V2]})
        mock.start(); self.addCleanup(mock.stop)
        self.writer = patch.object(food, "update_cat_profile").start()
        self.addCleanup(patch.stopall)

    def turn(self, message="", interaction=None, pet="pet"):
        self.state["turn_count"] += 1
        return food.handle_food_flow("user", {"pet_id": pet}, message, self.state, self.context, interaction)

    def select(self):
        return self.turn(interaction={"slot": "target_food", "value": "formula:2"})

    def test_ambiguous_versions_require_confirmation(self):
        result = self.turn("换成蓝氏猎鸟乳鸽")
        self.assertEqual(result["response_type"], "text_or_select")
        self.assertEqual(len(result["interaction"]["options"]), 2)
        self.writer.assert_not_called()
        self.assertNotIn("target", self.state["slots"][food.STATE_KEY])

    def test_cross_brand_comparison_and_plan_do_not_write_profile(self):
        result = self.select()
        self.assertEqual(result["result"]["food_flow"]["stage"], "planned")
        self.assertEqual(result["result"]["food_comparison"]["original_ingredients"], "鸡肉，米")
        self.assertIn("鸽肉", result["reply"])
        self.assertIn("未按年龄筛选", result["reply"])
        self.writer.assert_not_called()

    def test_select_recommendation_by_ordinal(self):
        self.state["slots"][food.STATE_KEY] = {"stage": "selection", "candidates": [NEW]}
        self.assertEqual(self.turn("选第一款")["result"]["food_flow"]["target"]["formula_id"], 2)

    def test_unknown_and_forged_target_ids_do_not_save(self):
        for name in ("不知道的品牌", "formula:9999"):
            result = self.turn(interaction={"slot": "target_food", "value": name})
            self.assertEqual(result["result"]["food_flow"]["stage"], "choose_target")
        self.writer.assert_not_called()

    def test_negative_future_and_questions_do_not_confirm(self):
        self.select()
        for message in ("还没开始吃", "准备已经开始吃这款", "已经开始吃这款吗？", "没有开始换粮"):
            self.turn(message)
        self.writer.assert_not_called()
        self.assertEqual(self.state["slots"][food.STATE_KEY]["stage"], "planned")

    def test_started_confirmation_is_idempotent_and_dates_unknown(self):
        self.select()
        for _ in range(2):
            result = self.turn("已经开始吃这款")
        self.writer.assert_called_once_with("user", "pet", {"food_product_id": 22, "food_formula_id": 2},
                                            expected_food={"food_brand": "原牌", "food_product": "原粮", "food_formula_id": 1})
        self.assertIsNone(result["result"]["food_flow"]["start_date"])
        result = self.turn("已经换好了")
        self.assertEqual(result["result"]["food_flow"]["stage"], "completed")
        self.turn("食欲正常")
        self.assertEqual(self.state["slots"][food.STATE_KEY]["latest_feedback"]["text"], "食欲正常")

    def test_profile_race_does_not_overwrite_new_current_food(self):
        self.select()
        self.context["pet_profile"]["food_formula_id"] = 9
        self.assertIn("档案已经变化", self.turn("已经开始吃这款")["reply"])
        self.writer.assert_not_called()

    def test_retry_after_profile_saved_recovers_without_second_write(self):
        self.select()
        self.context["pet_profile"]["food_formula_id"] = 2
        self.assertEqual(self.turn("已经开始吃这款")["result"]["food_flow"]["stage"], "started")
        self.writer.assert_not_called()

    def test_no_pet_cannot_confirm_feeding(self):
        self.select()
        self.assertIn("绑定宠物", self.turn("已经开始吃这款", pet=None)["reply"])
        self.writer.assert_not_called()

    def test_dogs_and_medical_diets_do_not_use_cat_plan(self):
        for pet in ({"animal_type": "dog"}, {"diseases": ["肾病"]}, {"allergies": ["未知过敏原"]}, {"age_months": 3}):
            self.context["pet_profile"] = pet
            result = self.select()
            self.assertNotIn("switch_plan", result["result"])
        self.writer.assert_not_called()

    def test_restart_recommendation_clears_only_plan(self):
        self.select()
        self.state["slots"]["_guardian_context"] = {"history": "旧粮原话"}
        self.assertIsNone(self.turn("重新推荐"))
        self.assertNotIn("target_food", self.state["slots"])
        self.assertEqual(self.state["slots"]["_guardian_context"]["history"], "旧粮原话")

    def test_started_flow_remains_routed_without_model_and_urgent_interrupts(self):
        self.select()
        with patch.object(chat, "_extract_intent", side_effect=AssertionError("unneeded model")):
            self.assertEqual(chat._recognize_guardian_turn("已经开始吃这款", self.state)[0]["_target_flow"], "food_switch")
            self.assertEqual(chat._recognize_guardian_turn("便血", self.state)[0]["_target_flow"], "soft_stool")

    def test_handoff_does_not_reset_ongoing_food_reason(self):
        self.state["slots"]["switch_reason"] = "skin"
        chat._prepare_food_handoff(self.state, "pet")
        self.assertEqual(self.state["slots"]["switch_reason"], "skin")

    def test_continuation_keeps_verified_original_ingredients(self):
        self.select()
        self.assertEqual(self.turn("还没开始吃")["result"]["food_comparison"]["original_ingredients"], "鸡肉，米")

    def test_choose_this_requires_specific_choice_when_multiple(self):
        self.state["slots"][food.STATE_KEY] = {"stage": "selection", "candidates": [NEW, V2]}
        self.assertEqual(len(self.turn("选这款")["interaction"]["options"]), 2)
        self.writer.assert_not_called()

    def test_allergy_change_after_plan_blocks_confirmation(self):
        self.select()
        self.context["pet_profile"]["allergies"] = ["未知过敏原"]
        self.turn("已经开始吃这款")
        self.writer.assert_not_called()

    def test_current_food_from_consultation_is_used_without_inventing_profile(self):
        self.context = {"pet_profile": {}, "current_food": None}
        self.state["slots"]["current_food"] = "原牌原粮"
        result = self.select()
        self.assertEqual(result["result"]["food_comparison"]["original_ingredients"], "鸡肉，米")
        self.writer.assert_not_called()

    def test_unrelated_question_can_leave_food_flow(self):
        self.select()
        with patch.object(chat, "_extract_intent", return_value=({"primary_intent": "general_pet_qa", "slots": {}}, None)):
            self.assertEqual(chat._recognize_guardian_turn("猫为什么会打呼噜", self.state)[0]["_target_flow"], "general")

    def test_selection_analyses_fit_before_transition_plan(self):
        self.state['slots']['_food_mode'] = 'selection'
        selected = self.select()
        self.assertEqual(selected['result']['food_flow']['stage'], 'selected')
        self.assertNotIn('switch_plan', selected['result'])
        self.writer.assert_not_called()
        planned = self.turn(interaction={'slot': 'food_flow_action', 'value': 'switch_plan'})
        self.assertEqual(planned['result']['food_flow']['stage'], 'planned')
        self.assertTrue(planned['result']['switch_plan'])
        self.writer.assert_not_called()

    def test_explicit_product_with_missing_type_and_stage_is_compared_with_warnings(self):
        food.list_standardized_product_options.return_value = {"items": [OLD, INCOMPLETE]}
        result = self.turn(interaction={"slot": "target_food", "value": "formula:4"})
        self.assertIn("资料提示", result["reply"])
        self.assertIn("产品类型资料缺失", result["reply"])
        self.assertIn("适用阶段资料缺失", result["reply"])
        self.assertEqual(result["result"]["food_flow"]["target"]["formula_id"], 4)
        self.assertTrue(result["result"]["switch_plan"])

    def test_explicit_product_still_blocks_known_age_conflict_and_allergen(self):
        kitten_food = {**NEW, "life_stage": "kitten", "ingredient_composition": "鸡肉，米"}
        food.list_standardized_product_options.return_value = {"items": [OLD, kitten_food]}
        self.context["pet_profile"].update(age_months=36)
        result = self.select()
        self.assertIn("年龄与产品明确标注的适用阶段不一致", result["reply"])
        self.context["pet_profile"].update(age_months=None, allergies=["鸡肉"])
        result = self.select()
        self.assertIn("配料表包含档案中的过敏原", result["reply"])
