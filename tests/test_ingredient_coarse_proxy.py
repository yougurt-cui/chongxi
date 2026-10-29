from services.fat_science_materialization_service import build_science_features
from services.fiber_science_materialization_service import build_science_payload
from services.ingredient_coarse_proxy_service import apply_proxy, classify, proxy_profile


def test_classifies_common_unmatched_families():
    assert classify("冻鸡肉")["category"] == "protein"
    assert classify("三文鱼油")["category"] == "fat"
    assert classify("菊粉")["category"] == "prebiotic"
    assert classify("木薯淀粉")["category"] == "carbohydrate"


def test_proxy_is_persistable_without_fake_standard_id():
    item = {"position": 1, "raw_name": "冻鸡肉", "standard_ingredient_id": None}
    apply_proxy(item)
    assert item["standard_ingredient_id"] is None
    assert item["primary_nutrition_role"] == "蛋白质供给"
    stored = {**item, "coarse_proxy": None, "features_json": {"coarse_proxy": item["coarse_proxy"]}}
    assert proxy_profile(stored)["nutrition_category"] == "protein"


def test_fat_and_fiber_materializers_accept_proxies_as_provisional_science():
    fat = {"position": 1, "raw_name": "鱼油", "standard_ingredient_id": None}
    starch = {"position": 2, "raw_name": "木薯淀粉", "standard_ingredient_id": None}
    apply_proxy(fat)
    apply_proxy(starch)
    fat_result = build_science_features([fat, starch], {})
    fiber_result = build_science_payload([fat, starch], {})
    assert fat_result["profile_status"] == "ready"
    assert fat_result["fat_source_types"] == "动物脂肪"
    assert fiber_result["profile_status"] == "ready"
    assert fiber_result["starch_ingredients_json"][0]["attribute_origin"] == "science_profile"
