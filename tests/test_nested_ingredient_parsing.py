import pytest
from services.catfood_standardization_service import normalize_ingredients
from scripts.backfill_formula_ingredient_features import protein_aggregate


@pytest.mark.parametrize('parser', [lambda text: normalize_ingredients(text)[1], protein_aggregate._split_ingredient_tokens])
@pytest.mark.parametrize('text,expected', [
    ('鲜鸡肉64.3%、鸡肉粉10.9%、红薯片、冻干生骨肉6%（鲜鸡肉90%、虾（冻南极磷虾）5.5%、菊苣菊糖、冻牡蛎1.5%、鱼（冻飞鱼子）1.0%）、马铃薯淀粉',
     ['鲜鸡肉', '鸡肉粉', '红薯片', '鲜鸡肉', '冻南极磷虾', '菊苣菊糖', '冻牡蛎', '冻飞鱼子', '马铃薯淀粉']),
    ('水解鱼蛋白粉(含鳕鱼胶原蛋白)0.35%、鸡油', ['水解鱼蛋白粉', '鸡油']),
    ('鸡肉（来源说明（冷冻保存）、产地）、鸡油', ['鸡肉', '鸡油']),
    ('冻干生骨肉6%(鲜鸡肉90%、虾(冻南极磷虾)5.5%、菊苣菊糖)、马铃薯淀粉',
     ['鲜鸡肉', '冻南极磷虾', '菊苣菊糖', '马铃薯淀粉']),
])
def test_nested_compounds_expand_without_leaking_annotations(parser, text, expected):
    assert parser(text) == expected
