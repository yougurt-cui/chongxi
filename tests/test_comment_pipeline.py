import unittest
from types import SimpleNamespace

from comment_pipeline.common.cleaner import clean_source_row, is_meaningful, normalize_text
from comment_pipeline.pipelines.product_preference_pipeline import fallback_extract
from comment_pipeline.router import route_comment


SPEC = SimpleNamespace(
    platform="xiaohongshu", table="xiaohongshu_raw_comments", id_col="id",
    external_id_col="external_id", title_col="title", content_col="content",
    like_col="like_count", time_col="comment_time", keyword_col="query_keyword",
)


def make_comment(text):
    return clean_source_row(SPEC, {
        "id": 1, "external_id": "x1", "title": "宠物用品分享", "content": "",
        "like_count": 3, "comment_time": "2026-09-28", "query_keyword": "宠物玩具",
        "comment_text": text,
    }, {"皇家": "皇家"})


class CommentPipelineTest(unittest.TestCase):
    def test_cleaner_keeps_raw_and_normalized_text(self):
        comment = make_comment("  皇家\u200b 猫粮   怎么选？ ")
        self.assertEqual(comment.raw_text, "  皇家\u200b 猫粮   怎么选？ ")
        self.assertEqual(comment.clean_text, "皇家 猫粮 怎么选？")
        self.assertEqual(comment.brand, "皇家")
        self.assertEqual(comment.product_category, "猫粮")

    def test_cleaner_filters_low_value_comments(self):
        self.assertFalse(is_meaningful(normalize_text("哈哈哈哈！")))
        self.assertIsNone(make_comment("收到"))

    def test_cleaner_prefers_standard_product_alias(self):
        row = {
            "id": 2, "external_id": "x2", "title": "", "content": "", "like_count": 0,
            "comment_time": "", "query_keyword": "", "comment_text": "我家正在吃皇家的肠胃舒适",
        }
        comment = clean_source_row(SPEC, row, {"皇家": "皇家"}, {"肠胃舒适": ("皇家肠胃舒适", "干粮")})
        self.assertEqual(comment.product_name, "皇家肠胃舒适")
        self.assertEqual(comment.product_category, "干粮")

    def test_router_can_match_both_pipelines(self):
        comment = make_comment("这款猫粮怎么选？另外逗猫棒的羽毛很结实，我家猫喜欢追着玩")
        route = route_comment(comment)
        self.assertTrue(route.choice)
        self.assertTrue(route.product_preference)

    def test_food_and_toy_domains_are_rule_routed_separately(self):
        food = route_comment(make_comment("这款猫粮颗粒太大，我家猫不爱吃，准备换粮"))
        self.assertTrue(food.choice)
        self.assertFalse(food.product_preference)
        toy = route_comment(make_comment("这个逗猫棒有羽毛，我家猫每天都喜欢追着玩"))
        self.assertFalse(toy.choice)
        self.assertTrue(toy.product_preference)

    def test_food_experience_is_rule_routed_to_choice_v2(self):
        food = route_comment(make_comment("皇家猫粮吃了三年，一直很稳定，适口性不错"))
        self.assertTrue(food.choice)
        self.assertFalse(food.product_preference)
        self.assertEqual(food.router_source, "rule")

    def test_toy_from_general_pet_brand_does_not_leak_into_choice(self):
        comment = make_comment("小佩的逗猫棒不错，我家猫很喜欢追")
        comment = comment.__class__(**{**comment.__dict__, "brand": "小佩"})
        route = route_comment(comment)
        self.assertFalse(route.choice)
        self.assertTrue(route.product_preference)

    def test_reserved_medical_and_monitoring_domains_do_not_leak(self):
        medical = route_comment(make_comment("这个宠物雾化器声音太大，猫很不喜欢"))
        monitor = route_comment(make_comment("智能项圈定位器太重，猫不愿意戴"))
        self.assertFalse(medical.choice or medical.product_preference)
        self.assertFalse(monitor.choice or monitor.product_preference)

    def test_router_skips_clearly_irrelevant_comment_without_llm(self):
        comment = make_comment("今天下雨，记得带伞")
        route = route_comment(comment)
        self.assertFalse(route.choice)
        self.assertFalse(route.product_preference)

    def test_generic_this_comment_is_not_product_context(self):
        row = {
            "id": 3, "external_id": "x3", "title": "家居分享", "content": "收纳改造",
            "like_count": 0, "comment_time": "", "query_keyword": "厨房",
            "comment_text": "我超喜欢你这个，但价格有点贵",
        }
        route = route_comment(clean_source_row(SPEC, row, {}))
        self.assertFalse(route.product_preference)

    def test_ambiguous_router_uses_llm_multilabel_result(self):
        class FakeLlm:
            available = True

            def complete(self, _system_prompt, _payload):
                return {"choice": True, "product_preference": True}

        route = route_comment(make_comment("我家猫最近有点纠结"), FakeLlm())
        self.assertTrue(route.choice)
        self.assertTrue(route.product_preference)
        self.assertEqual(route.router_source, "llm")

    def test_preference_fallback_extracts_example(self):
        comment = make_comment("这个毛绒小鱼我家猫特别喜欢抱着蹬，但是玩两天线头就出来了")
        event = fallback_extract(comment)
        self.assertEqual(event["preference"], "positive")
        self.assertEqual(event["durability"], "较差")
        self.assertIn("毛绒", event["material"])
        self.assertIn("蹬", event["pet_action"])

    def test_negative_preference_does_not_match_positive_substring(self):
        event = fallback_extract(make_comment("这个玩具我家猫不喜欢，也完全不爱玩"))
        self.assertEqual(event["preference"], "negative")

    def test_negative_interest_and_time_phrase_are_not_misclassified(self):
        event = fallback_extract(make_comment("我家猫对逗猫棒不感兴趣，这两天准备换一个"))
        self.assertEqual(event["preference"], "negative")
        self.assertEqual(event["durability"], "")

    def test_breed_name_is_not_material(self):
        event = fallback_extract(make_comment("我家布偶猫不喜欢这个玩具"))
        self.assertNotIn("布", event["material"])

    def test_product_name_and_pet_symptom_are_not_preference_signals(self):
        self.assertFalse(route_comment(make_comment("猫抓板求链接")).product_preference)
        self.assertFalse(route_comment(make_comment("猫咪最近掉毛，是猫粮问题吗")).product_preference)

    def test_preference_converges_structure_interaction_benefit_and_proof(self):
        event = fallback_extract(make_comment(
            "这个自动球带绳子和老鼠挂件，会发出沙沙声。猫叼着满屋跑，每天自己玩很久，"
            "停了还要我打开，就是声音太大，已经玩烂了两个。"
        ))
        self.assertIn("自动移动结构", event["structure"])
        self.assertIn("绳状附件", event["structure"])
        self.assertIn("鸟类/老鼠类挂件", event["structure"])
        self.assertIn("叼着移动", event["interaction"])
        self.assertIn("提高自主玩耍时间", event["benefit"])
        self.assertIn("每天都玩", event["user_proof"])
        self.assertIn("运行声音较大", event["pain_point"])
        self.assertIn("耐用性不足", event["pain_point"])


if __name__ == "__main__":
    unittest.main()
