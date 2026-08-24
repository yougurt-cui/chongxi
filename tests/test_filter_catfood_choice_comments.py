from scripts.filter_catfood_choice_comments import condition_metadata, is_choice_comment


def test_symptom_demand_is_kept_without_food_or_brand_context():
    keep, _signals, intents, _score, mentions_brand, mentions_condition = is_choice_comment(
        "我家猫最近一直软便怎么办"
    )

    assert keep is True
    assert "Need" in intents
    assert mentions_brand is False
    assert mentions_condition is True


def test_unrelated_comment_is_still_rejected():
    keep, *_rest = is_choice_comment("今天天气很好，出去散步了")

    assert keep is False


def test_urinary_condition_is_recalled():
    keep, _signals, intents, _score, _brand, condition = is_choice_comment("最近频繁蹲猫砂盆但尿不出来")

    assert keep is True
    assert "Need" in intents
    assert condition is True
    metadata = condition_metadata("最近频繁蹲猫砂盆但尿不出来")
    assert metadata["confidence"] == "high"
    assert metadata["categories"] == "泌尿系统问题"
    assert metadata["symptoms"] == "尿结晶/尿路问题"


def test_ambiguous_condition_uses_parent_post_context_for_confidence():
    assert condition_metadata("吃完总是吐")["confidence"] == "low"
    metadata = condition_metadata("吃完总是吐", "布偶幼猫猫粮使用反馈")
    assert metadata["confidence"] == "high"


def test_specific_condition_without_context_is_medium_confidence():
    metadata = condition_metadata("最近出现尿路结晶")

    assert metadata["confidence"] == "medium"


def test_weight_gain_demand_is_distinct_from_obesity():
    metadata = condition_metadata("我家幼猫吃什么粮可以长肉发腮")

    assert metadata["confidence"] == "high"
    assert metadata["categories"] == "体重与代谢问题"
    assert metadata["symptoms"] == "增重/长肉"


def test_not_gaining_weight_is_not_classified_as_weight_gain_goal():
    metadata = condition_metadata("我家猫一直不长肉，还是太瘦")

    assert metadata["confidence"] == "high"
    assert metadata["symptoms"] == "消瘦/不长肉"


def test_generic_tearing_is_not_recalled_as_tear_stain():
    assert condition_metadata("看完以后感动得一直流泪")["mentions_condition"] is False
    assert condition_metadata("最近眼泪很多")["mentions_condition"] is False


def test_pet_specific_tear_stain_signals_are_still_recalled():
    metadata = condition_metadata("我家猫最近泪痕和眼屎比较多")

    assert metadata["confidence"] == "high"
    assert metadata["categories"] == "眼部问题"
    assert metadata["symptoms"] == "泪痕"
