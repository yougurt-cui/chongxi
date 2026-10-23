"""健康关注方向规则（演示阈值，未经临床验证，不用于诊断）。

移植自 pet_health_engine_v2.zip 的 rules.py，改动：
  - 新增 `food_dispensed_portions`（出粮份数，单位“份”）：小佩喂食器 API 只报“份数”不报克数，
    不能伪装成 food_dispensed(g)，更不能冒充 food_intake，因此单独立指标。
"""

RULE_VERSION = "demo_v1"

METRICS = {
    "food_intake": ("进食量", "g"),
    "food_frequency": ("进食次数", "次"),
    "food_dispensed": ("出粮量", "g"),
    "food_dispensed_portions": ("出粮份数", "份"),
    "water_intake": ("饮水量", "mL"),
    "urine_frequency": ("排尿次数", "次"),
    "urine_volume": ("单次尿量", "mL"),
    "stool_frequency": ("排便次数", "次"),
    "day_activity": ("白天活动量", "分钟"),
    "night_activity": ("夜间活动量", "分钟"),
    "weight": ("体重", "kg"),
    "temperature": ("体温", "℃"),
    "resting_respiration": ("静息呼吸频率", "次/分钟"),
}

THRESHOLDS = {
    "food_intake": 0.25,
    "food_frequency": 0.30,
    "water_intake": 0.30,
    "urine_frequency": 0.50,
    "urine_volume": 0.30,
    "stool_frequency": 0.50,
    "day_activity": 0.30,
    "night_activity": 0.35,
    "weight": 0.05,
    "temperature": 0.05,
    "resting_respiration": 0.30,
}

DIRECTIONS = {
    "stress": {
        "name": "应激与情绪",
        "core": ["food_intake", "food_frequency", "day_activity", "night_activity"],
        "aux": ["urine_frequency"],
        "abnormal": "进食或活动规律有变化，留意应激及身体不适。",
        "normal": "已监测的进食及活动规律平稳，仍需留意互动变化。",
        "missing": "缺少进食或活动记录，留意食欲、躲藏和互动变化。",
    },
    "gastrointestinal": {
        "name": "胃肠道健康",
        "core": ["food_intake", "stool_frequency"],
        "aux": ["water_intake", "weight"],
        "abnormal": "饮食或排便有变化，留意软便、呕吐等情况。",
        "normal": "已监测的饮食及排便习惯平稳，留意便便形态。",
        "missing": "缺少饮食或排便记录，留意便便形态与食欲。",
    },
    "urinary": {
        "name": "泌尿道健康",
        "core": ["urine_frequency", "urine_volume"],
        "aux": ["water_intake", "food_intake"],
        "abnormal": "排尿习惯有变化，留意尿量及排尿是否顺畅。",
        "normal": "已监测的排尿习惯平稳，仍需留意排尿行为。",
        "missing": "缺少排尿数据，留意尿团大小及排尿是否顺畅。",
    },
    "mobility": {
        "name": "关节与运动",
        "core": ["day_activity", "night_activity"],
        "aux": ["weight"],
        "abnormal": "活动量或节律变化，留意跳跃、行走是否吃力。",
        "normal": "已监测的活动习惯平稳，留意跳跃与步态。",
        "missing": "缺少活动数据，留意跳跃和行走变化。",
    },
    "inflammation": {
        "name": "感染与炎症",
        "core": ["temperature", "food_intake", "day_activity"],
        "aux": ["weight", "resting_respiration"],
        "abnormal": "体温或日常状态变化，建议结合症状咨询兽医。",
        "normal": "已记录体征未见明显变化，但不能排除炎症。",
        "missing": "缺少足够体征数据，留意食欲、精神和体温变化。",
    },
}

# 完整覆盖最低要求；某些核心数据缺失时，不能用“正常”代替未评估。
REQUIRED = {
    "stress": ["food_intake", "day_activity"],
    "gastrointestinal": ["food_intake", "stool_frequency"],
    "urinary": ["urine_frequency", "urine_volume"],
    "mobility": ["day_activity", "night_activity"],
    "inflammation": ["temperature", "food_intake", "day_activity"],
}
