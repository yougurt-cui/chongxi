# process_pressure_inference_qwen.py
# -*- coding: utf-8 -*-

"""
功能：
输入“原料路径变化信号”，输出：
1. 工艺压力判断
2. 推荐观测指标
3. 通义千问生成的总结文本

核心设计：
- 规则引擎负责判断工艺压力和推荐指标
- 通义千问只负责把结构化结果总结成报告话术
- 避免大模型直接自由判断，减少幻觉

依赖安装：
pip install openai

环境变量：
export DASHSCOPE_API_KEY="你的阿里云百炼API Key"
export QWEN_MODEL="qwen-plus"

Windows PowerShell:
$env:DASHSCOPE_API_KEY="你的阿里云百炼API Key"
$env:QWEN_MODEL="qwen-plus"
"""

import os
import json
from typing import List, Dict, Any, Tuple


# =========================================================
# 1. 工艺压力类型
# =========================================================

PRESSURE_LABELS = {
    "water_distribution_pressure": "水分分布压力",
    "protein_dispersion_pressure": "蛋白分散压力",
    "fiber_dispersion_pressure": "纤维分散压力",
    "cutting_forming_pressure": "成型切割压力",
    "particle_structure_pressure": "颗粒结构压力",
    "starch_extrusion_pressure": "淀粉糊化成型压力",
    "oil_oxidation_pressure": "油脂喷涂/氧化压力",
    "functional_micro_ingredient_pressure": "功能小料分散压力",
}


# =========================================================
# 2. 工艺压力 -> 推荐观测指标
# =========================================================

PRESSURE_TO_INDICATORS = {
    "water_distribution_pressure": [
        {
            "indicator": "混合后水分CV",
            "indicator_type": "混合均匀性",
            "expected_direction": "上升风险",
            "reason": "湿态蛋白、发酵纤维或薯类淀粉会改变吸水速度和保水能力，混合阶段更容易形成局部高水分或低水分区，因此水分CV存在上升风险。",
        }
    ],
    "protein_dispersion_pressure": [
        {
            "indicator": "混合后粗蛋白CV",
            "indicator_type": "混合均匀性",
            "expected_direction": "上升风险",
            "reason": "蛋白来源增多或粉体/湿料形态差异变大时，各蛋白原料的粒径、密度和黏附性不同，混合后粗蛋白分布更容易不均，因此粗蛋白CV存在上升风险。",
        }
    ],
    "fiber_dispersion_pressure": [
        {
            "indicator": "粗纤维CV",
            "indicator_type": "混合均匀性",
            "expected_direction": "上升风险",
            "reason": "结构纤维或复合纤维增加后，纤维粒径、密度和吸水膨胀行为差异更明显，混合中更容易分层或局部富集，因此粗纤维CV存在上升风险。",
        }
    ],
    "cutting_forming_pressure": [
        {
            "indicator": "切口完整率",
            "indicator_type": "成型稳定性",
            "expected_direction": "下降风险",
            "reason": "高吸水纤维、湿态蛋白或油脂负担会改变物料黏弹性和出料连续性，切割时更容易拖尾、粘刀或断面破碎，因此切口完整率存在下降风险。",
        }
    ],
    "particle_structure_pressure": [
        {
            "indicator": "颗粒表面粗糙度",
            "indicator_type": "颗粒结构稳定性",
            "expected_direction": "上升风险",
            "reason": "结构纤维、蛋白粉或淀粉糊化波动会让颗粒内部结合不均，粗颗粒原料也更容易外露，因此颗粒表面粗糙度存在上升风险。",
        },
        {
            "indicator": "粉化率",
            "indicator_type": "颗粒结构稳定性",
            "expected_direction": "上升风险",
            "reason": "纤维增加、蛋白粉分散不足或淀粉糊化支撑下降时，颗粒结合力和抗破碎能力会变弱，因此运输和摩擦后的粉化率存在上升风险。",
        },
    ],
    "starch_extrusion_pressure": [
        {
            "indicator": "淀粉糊化度",
            "indicator_type": "膨化熟化",
            "expected_direction": "下降风险",
            "reason": "豆类、薯类和纯淀粉的糊化温度、吸水速度和颗粒结构不同，原有膨化参数可能无法充分适配新碳水路径，因此淀粉糊化度存在下降风险。",
        },
        {
            "indicator": "膨化度",
            "indicator_type": "膨化成型",
            "expected_direction": "波动风险",
            "reason": "碳水来源变化会改变熔融黏度、蒸汽释放和膨化窗口，可能出现膨化不足或过膨化两种方向，因此膨化度存在波动风险。",
        },
        {
            "indicator": "颗粒硬度",
            "indicator_type": "颗粒结构稳定性",
            "expected_direction": "波动风险",
            "reason": "淀粉糊化程度、纤维吸水和干燥曲线共同决定颗粒致密度；路径变化后可能过硬，也可能因结合不足而变脆，因此颗粒硬度存在波动风险。",
        },
    ],
    "oil_oxidation_pressure": [
        {
            "indicator": "表面残油",
            "indicator_type": "油脂后喷涂",
            "expected_direction": "上升风险",
            "reason": "植物油、功能油脂或后喷比例增加时，颗粒吸油容量可能不足，油脂更容易停留在颗粒表面，因此表面残油存在上升风险。",
        },
        {
            "indicator": "过氧化值",
            "indicator_type": "油脂氧化稳定性",
            "expected_direction": "上升风险",
            "reason": "不饱和油脂路径增加后氧化敏感性提高；若抗氧化剂分散或覆盖不足，初级氧化产物更容易累积，因此过氧化值存在上升风险。",
        },
        {
            "indicator": "酸价",
            "indicator_type": "油脂氧化稳定性",
            "expected_direction": "上升风险",
            "reason": "油脂来源、喷涂温度和储存水分活度变化会放大水解劣变风险，游离脂肪酸可能增加，因此酸价存在上升风险。",
        },
    ],
    "functional_micro_ingredient_pressure": [
        {
            "indicator": "功能成分混合均匀性",
            "indicator_type": "小料分散",
            "expected_direction": "波动风险",
            "reason": "益生元、抗氧化剂等功能小料添加量低，且粒径、吸湿性和载体差异明显，局部浓度可能偏高或偏低，因此功能成分混合均匀性存在波动风险。",
        },
        {
            "indicator": "留样稳定性",
            "indicator_type": "储存稳定性",
            "expected_direction": "下降风险",
            "reason": "功能小料、油脂和抗氧化体系变化后，储存期内可能出现气味衰减、氧化加快或活性损失，因此留样稳定性存在下降风险。",
        },
    ],
}


# =========================================================
# 3. 原料路径变化规则库
# =========================================================

RULES = [
    {
        "rule_id": "protein_meal_to_fresh_meat",
        "rule_name": "肉粉变鲜肉",
        "from_keywords": [
            "肉粉", "鸡肉粉", "鸭肉粉", "鱼粉", "禽肉粉", "牛肉粉",
            "羊肉粉", "三文鱼粉", "白鱼粉"
        ],
        "to_keywords": [
            "鲜肉", "鲜鸡肉", "鲜鸭肉", "鲜鱼", "鲜牛肉", "鲜羊肉",
            "冻肉", "鸡肉", "鸭肉", "鱼肉", "肉浆", "肉糜"
        ],
        "material_change": "干粉蛋白转为湿态蛋白，水分、黏性和蛋白分散难度上升。",
        "pressure_scores": {
            "water_distribution_pressure": 3,
            "protein_dispersion_pressure": 3,
            "cutting_forming_pressure": 2,
            "particle_structure_pressure": 1,
        },
    },
    {
        "rule_id": "fresh_meat_to_protein_meal",
        "rule_name": "鲜肉变肉粉",
        "from_keywords": [
            "鲜肉", "鲜鸡肉", "鲜鸭肉", "鲜鱼", "鲜牛肉", "鲜羊肉",
            "冻肉", "鸡肉", "鸭肉", "鱼肉", "肉浆", "肉糜"
        ],
        "to_keywords": [
            "肉粉", "鸡肉粉", "鸭肉粉", "鱼粉", "禽肉粉", "牛肉粉",
            "羊肉粉", "三文鱼粉", "白鱼粉"
        ],
        "material_change": "湿态蛋白转为干粉蛋白，水分分布压力下降，但粉体分散和蛋白均匀性仍需关注。",
        "pressure_scores": {
            "protein_dispersion_pressure": 1,
            "particle_structure_pressure": 1,
        },
    },
    {
        "rule_id": "soluble_to_insoluble_fiber",
        "rule_name": "可溶纤维变不可溶纤维",
        "from_keywords": [
            "可溶纤维", "菊粉", "低聚果糖", "FOS", "果寡糖",
            "低聚半乳糖", "GOS", "车前子", "车前子壳", "瓜尔胶"
        ],
        "to_keywords": [
            "不可溶纤维", "纤维素", "豌豆纤维", "甜菜粕",
            "苹果纤维", "竹纤维", "木质纤维", "燕麦纤维"
        ],
        "material_change": "增稠/持水型纤维转为结构骨架型纤维，粒径敏感性、分层风险和颗粒结构压力上升。",
        "pressure_scores": {
            "fiber_dispersion_pressure": 3,
            "cutting_forming_pressure": 2,
            "particle_structure_pressure": 3,
        },
    },
    {
        "rule_id": "insoluble_to_soluble_fiber",
        "rule_name": "不可溶纤维变可溶纤维",
        "from_keywords": [
            "不可溶纤维", "纤维素", "豌豆纤维", "甜菜粕",
            "苹果纤维", "竹纤维", "木质纤维", "燕麦纤维"
        ],
        "to_keywords": [
            "可溶纤维", "菊粉", "低聚果糖", "FOS", "果寡糖",
            "低聚半乳糖", "GOS", "车前子", "车前子壳", "瓜尔胶"
        ],
        "material_change": "结构骨架型纤维转为增稠/持水型纤维，颗粒粗糙和粉化压力可能下降，但水分分布和物料黏性压力可能上升。",
        "pressure_scores": {
            "water_distribution_pressure": 2,
            "cutting_forming_pressure": 1,
        },
    },
    {
        "rule_id": "single_to_multiple_protein",
        "rule_name": "单一蛋白变多蛋白",
        "from_keywords": [
            "单一蛋白", "单一肉源", "单一动物蛋白"
        ],
        "to_keywords": [
            "多蛋白", "多肉源", "复合蛋白", "多动物蛋白",
            "鸡肉+鱼肉", "鸡肉+鸭肉", "鸡肉+牛肉", "鸭肉+鱼肉"
        ],
        "material_change": "蛋白来源增加，原料粒径、密度、吸水性和热变性行为差异变大。",
        "pressure_scores": {
            "protein_dispersion_pressure": 2,
            "particle_structure_pressure": 1,
        },
    },
    {
        "rule_id": "multiple_to_single_protein",
        "rule_name": "多蛋白变单一蛋白",
        "from_keywords": [
            "多蛋白", "多肉源", "复合蛋白", "多动物蛋白",
            "鸡肉+鱼肉", "鸡肉+鸭肉", "鸡肉+牛肉", "鸭肉+鱼肉"
        ],
        "to_keywords": [
            "单一蛋白", "单一肉源", "单一动物蛋白"
        ],
        "material_change": "蛋白来源复杂度下降，蛋白分散压力可能下降。",
        "pressure_scores": {
            "protein_dispersion_pressure": 1,
        },
    },
    {
        "rule_id": "fiber_complexity_increase",
        "rule_name": "纤维体系复杂度上升",
        "from_keywords": [
            "单一纤维", "低纤维", "少量纤维", "基础纤维"
        ],
        "to_keywords": [
            "复合纤维", "多纤维", "高纤维", "纤维体系增强",
            "不可溶纤维增加", "纤维复配"
        ],
        "material_change": "纤维体系复杂度上升，分散均匀性和颗粒结构稳定性压力增加。",
        "pressure_scores": {
            "fiber_dispersion_pressure": 2,
            "cutting_forming_pressure": 1,
            "particle_structure_pressure": 2,
        },
    },
    {
        "rule_id": "prebiotic_complexity_increase",
        "rule_name": "益生元体系复杂度上升",
        "from_keywords": [
            "单一益生元", "无益生元", "低益生元"
        ],
        "to_keywords": [
            "多益生元", "复合益生元", "益生元复配",
            "菊粉+果寡糖", "MOS", "FOS", "低聚果糖+甘露寡糖"
        ],
        "material_change": "益生元体系复杂度上升，可能增加小料分散压力；若缺少专项检测，可优先用粗纤维CV或颗粒结构指标做间接观察。",
        "pressure_scores": {
            "fiber_dispersion_pressure": 1,
            "particle_structure_pressure": 1,
        },
    },
    {
        "rule_id": "fiber_insufficient_to_structural_or_fermentable",
        "rule_name": "纤维成形不足转为结构/发酵纤维",
        "from_keywords": [
            "纤维成形不足路径", "低纤维", "基础纤维", "果蔬纤维路径"
        ],
        "to_keywords": [
            "不可溶纤维路径", "不溶性纤维支撑路径", "发酵型纤维路径",
            "可发酵", "成形纤维路径", "甜菜粕", "车前子", "菊苣根"
        ],
        "material_change": "弱纤维托底转为结构型或发酵成形纤维，吸水膨胀、粉体分散和颗粒结构稳定性压力上升。",
        "pressure_scores": {
            "fiber_dispersion_pressure": 2,
            "water_distribution_pressure": 1,
            "cutting_forming_pressure": 1,
            "particle_structure_pressure": 2,
        },
    },
    {
        "rule_id": "fermentable_fiber_path_added",
        "rule_name": "发酵型纤维路径增加",
        "from_keywords": [
            "纤维成形不足路径", "果蔬纤维路径", "基础纤维", "低纤维"
        ],
        "to_keywords": [
            "发酵型纤维路径", "可发酵", "成形纤维路径", "甜菜粕",
            "菊苣", "菊粉", "车前子", "果胶"
        ],
        "material_change": "发酵/成形纤维增加，可能改变物料吸水、黏结和干燥负担。",
        "pressure_scores": {
            "water_distribution_pressure": 1,
            "fiber_dispersion_pressure": 1,
            "cutting_forming_pressure": 1,
            "particle_structure_pressure": 1,
        },
    },
    {
        "rule_id": "legume_carb_to_tuber_starch",
        "rule_name": "豆类碳水转为薯类/块茎碳水",
        "from_keywords": [
            "豆类碳水路径", "豆类淀粉路径", "豌豆", "鹰嘴豆", "扁豆", "兵豆"
        ],
        "to_keywords": [
            "薯类/块茎路径", "薯类", "块茎", "马铃薯", "土豆", "红薯", "甘薯", "木薯"
        ],
        "material_change": "豆类碳水转为薯类/块茎碳水，淀粉糊化窗口、水分迁移和膨化成型行为发生变化。",
        "pressure_scores": {
            "starch_extrusion_pressure": 3,
            "water_distribution_pressure": 1,
            "particle_structure_pressure": 1,
        },
    },
    {
        "rule_id": "omega6_plant_oil_path_added",
        "rule_name": "植物油/Omega-6路径增加",
        "from_keywords": [
            "动物脂肪路径", "鱼油/Omega-3路径", "鱼油", "动物脂肪"
        ],
        "to_keywords": [
            "植物油/Omega-6路径", "植物油", "Omega-6", "葵花籽油", "亚麻籽油"
        ],
        "material_change": "植物油或Omega-6路径增加，后喷负担、不饱和脂肪氧化和表油残留风险上升。",
        "pressure_scores": {
            "oil_oxidation_pressure": 3,
            "cutting_forming_pressure": 1,
        },
    },
    {
        "rule_id": "prebiotic_specific_path_added",
        "rule_name": "明确益生元路径增加",
        "from_keywords": [
            "益生元常规路径", "无益生元", "低益生元", "基础益生元"
        ],
        "to_keywords": [
            "FOS", "菊粉", "低聚果糖", "菊苣根路径", "益生元", "发酵底物"
        ],
        "material_change": "明确益生元/发酵底物路径增加，低添加量小料分散、吸湿和批次一致性需要关注。",
        "pressure_scores": {
            "functional_micro_ingredient_pressure": 2,
            "fiber_dispersion_pressure": 1,
            "water_distribution_pressure": 1,
        },
    },
    {
        "rule_id": "antioxidant_system_path_added",
        "rule_name": "抗氧化体系覆盖增加",
        "from_keywords": [
            "天然果蔬抗氧化辅助路径", "莓果/植化路径", "基础抗氧化", "抗氧化不足"
        ],
        "to_keywords": [
            "抗氧化体系覆盖路径", "维生素E", "生育酚", "迷迭香", "迷叠香",
            "茶多酚", "天然抗氧化物", "丝兰提取物"
        ],
        "material_change": "抗氧化体系由天然果蔬辅助扩展为明确抗氧化覆盖，需关注小料分散和油脂氧化保护稳定性。",
        "pressure_scores": {
            "functional_micro_ingredient_pressure": 1,
            "oil_oxidation_pressure": 2,
        },
    },
    {
        "rule_id": "animal_protein_complexity_priority_increase",
        "rule_name": "多动物蛋白复杂度上升",
        "from_keywords": [
            "肉粉浓缩蛋白路径", "单一动物蛋白", "基础蛋白路径"
        ],
        "to_keywords": [
            "多动物蛋白复杂特征", "多动物蛋白", "多肉源", "复合蛋白"
        ],
        "material_change": "动物蛋白来源复杂度上升，粒径、密度、吸水性和热变性行为差异变大。",
        "pressure_scores": {
            "protein_dispersion_pressure": 2,
            "particle_structure_pressure": 1,
        },
    },
]


# =========================================================
# 4. 工具函数
# =========================================================

def contains_any(text: str, keywords: List[str]) -> bool:
    """判断文本中是否包含任意关键词。"""
    text = text or ""
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)


def pressure_level(score: int) -> str:
    """压力分转压力等级。"""
    if score >= 4:
        return "高"
    if score >= 2:
        return "中"
    if score == 1:
        return "低"
    return "无明显"


def priority_from_score(score: int) -> str:
    """推荐指标优先级。"""
    if score >= 4:
        return "高"
    if score >= 2:
        return "中"
    return "低"


def normalize_signal(signal: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    支持两种输入：
    1. {"from": "肉粉", "to": "鲜肉"}
    2. {"change_signal": "肉粉 -> 鲜肉"}
    """

    from_material = signal.get("from", "") or signal.get("old_path", "")
    to_material = signal.get("to", "") or signal.get("new_path", "")
    raw_signal = signal.get("change_signal", "")

    if not from_material and not to_material and "->" in raw_signal:
        parts = raw_signal.split("->", 1)
        from_material = parts[0].strip()
        to_material = parts[1].strip()

    if not from_material and not to_material and "→" in raw_signal:
        parts = raw_signal.split("→", 1)
        from_material = parts[0].strip()
        to_material = parts[1].strip()

    if not raw_signal:
        raw_signal = f"{from_material} -> {to_material}"

    return from_material, to_material, raw_signal


def deduplicate_reasons(reasons: List[str]) -> List[str]:
    """原因去重。"""
    seen = set()
    result = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            result.append(reason)
    return result


# =========================================================
# 5. 规则推断主函数
# =========================================================

def infer_process_pressure(change_signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    输入原料路径变化信号，输出工艺压力和推荐指标。
    """

    pressure_scores = {key: 0 for key in PRESSURE_LABELS.keys()}
    pressure_reasons = {key: [] for key in PRESSURE_LABELS.keys()}
    matched_rules = []

    for signal in change_signals:
        from_material, to_material, raw_signal = normalize_signal(signal)
        has_structured_material = bool(from_material or to_material)

        for rule in RULES:
            from_hit = contains_any(from_material, rule["from_keywords"])
            to_hit = contains_any(to_material, rule["to_keywords"])

            # 兼容只有 change_signal 的情况；如果已经有结构化 from/to，
            # 不再用整句 raw_signal 反向匹配，避免“鲜肉 -> 肉粉”同时命中正反两条规则。
            raw_from_hit = not has_structured_material and contains_any(raw_signal, rule["from_keywords"])
            raw_to_hit = not has_structured_material and contains_any(raw_signal, rule["to_keywords"])

            if (from_hit and to_hit) or (raw_from_hit and raw_to_hit):
                matched_rules.append({
                    "input_signal": raw_signal,
                    "rule_id": rule["rule_id"],
                    "rule_name": rule["rule_name"],
                    "material_change": rule["material_change"],
                    "pressure_scores": rule["pressure_scores"],
                })

                for pressure_key, score in rule["pressure_scores"].items():
                    pressure_scores[pressure_key] += score
                    pressure_reasons[pressure_key].append(
                        f"{rule['rule_name']}：{rule['material_change']}"
                    )

    process_pressures = []

    for key, score in pressure_scores.items():
        if score > 0:
            process_pressures.append({
                "pressure_key": key,
                "pressure_name": PRESSURE_LABELS[key],
                "score": score,
                "level": pressure_level(score),
                "reasons": deduplicate_reasons(pressure_reasons[key]),
            })

    # 推荐指标去重、合并
    indicator_map: Dict[str, Dict[str, Any]] = {}

    for pressure in process_pressures:
        pressure_key = pressure["pressure_key"]
        pressure_name = pressure["pressure_name"]
        pressure_score = pressure["score"]

        for item in PRESSURE_TO_INDICATORS.get(pressure_key, []):
            indicator = item["indicator"]

            if indicator not in indicator_map:
                indicator_map[indicator] = {
                    "indicator": indicator,
                    "indicator_type": item["indicator_type"],
                    "expected_direction": item["expected_direction"],
                    "reason": item["reason"],
                    "linked_pressures": [pressure_name],
                    "priority_score": pressure_score,
                    "priority": priority_from_score(pressure_score),
                }
            else:
                indicator_map[indicator]["linked_pressures"].append(pressure_name)
                indicator_map[indicator]["priority_score"] += pressure_score
                indicator_map[indicator]["priority"] = priority_from_score(
                    indicator_map[indicator]["priority_score"]
                )

    recommended_indicators = sorted(
        indicator_map.values(),
        key=lambda x: x["priority_score"],
        reverse=True
    )

    return {
        "input_signals": change_signals,
        "matched_rules": matched_rules,
        "process_pressures": process_pressures,
        "recommended_indicators": recommended_indicators,
    }


# =========================================================
# 6. 规则版总结兜底
# =========================================================

def fallback_summary(result: Dict[str, Any]) -> str:
    """不用大模型时的兜底总结。"""

    signals = []
    for signal in result.get("input_signals", []):
        _, _, raw_signal = normalize_signal(signal)
        signals.append(raw_signal)

    pressures = result.get("process_pressures", [])
    indicators = result.get("recommended_indicators", [])

    pressure_text = "、".join(
        [f"{p['pressure_name']}（{p['level']}）" for p in pressures]
    ) or "无明显工艺压力"

    indicator_text = "、".join(
        [
            f"{i['indicator']}（{i['expected_direction']}）"
            for i in indicators
        ]
    ) or "暂无推荐指标"

    return f"""
1. 原料路径变化
本次识别到的原料路径变化为：{"；".join(signals)}。

2. 工艺压力判断
规则引擎判断该变化可能带来：{pressure_text}。该判断用于提示小试/中试阶段的验证重点，不代表已经确定某个工艺环节异常。

3. 推荐观测指标
建议优先观测：{indicator_text}。

4. 验证建议
若混合后水分CV、混合后粗蛋白CV或粗纤维CV升高，优先关注混合均匀性；若切口完整率下降，优先关注出料成型稳定性；若颗粒表面粗糙度或粉化率升高，优先关注颗粒结构稳定性。
""".strip()


# =========================================================
# 7. 通义千问总结
# =========================================================

def summarize_with_qwen(result: Dict[str, Any]) -> str:
    """
    使用通义千问 OpenAI 兼容接口生成总结。

    依赖：
    pip install openai

    环境变量：
    DASHSCOPE_API_KEY=你的阿里云百炼API Key
    QWEN_MODEL=qwen-plus
    """

    api_key = os.getenv("DASHSCOPE_API_KEY")
    model = os.getenv("QWEN_MODEL", "qwen-plus")

    if not api_key:
        return fallback_summary(result)

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        system_prompt = """
你是宠物食品代工厂工艺分析顾问。
你需要基于规则引擎结果，生成面向猫粮代工厂研发/工艺团队的专业总结。

严格要求：
1. 不要说“已经确定某工艺出错”。
2. 要说“存在某类工艺压力，建议优先观测某些指标”。
3. 不要编造规则结果里没有的指标。
4. 不要扩展到规则结果之外的工艺环节。
5. 语言要专业、简洁，适合放在B端产品报告里。
6. 控制在300字以内。
"""

        user_prompt = f"""
请基于下面的规则引擎结果，输出结构化总结。

输出结构：
1. 原料路径变化
2. 工艺压力判断
3. 推荐观测指标
4. 验证建议

规则引擎结果：
{json.dumps(result, ensure_ascii=False, indent=2)}
"""

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": user_prompt.strip()},
            ],
            temperature=0.2,
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        return fallback_summary(result) + f"\n\n[通义千问调用失败，已返回规则版总结。错误：{str(e)}]"


# =========================================================
# 8. 示例运行
# =========================================================

def main():
    example_signals = [
        {
            "change_signal": "肉粉 -> 鲜肉",
            "from": "肉粉",
            "to": "鲜肉",
        },
        {
            "change_signal": "可溶纤维 -> 不可溶纤维",
            "from": "可溶纤维",
            "to": "不可溶纤维",
        },
    ]

    result = infer_process_pressure(example_signals)

    print("\n====== 结构化推断结果 ======")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print("\n====== 通义千问总结 / 规则兜底总结 ======")
    print(summarize_with_qwen(result))


if __name__ == "__main__":
    main()
