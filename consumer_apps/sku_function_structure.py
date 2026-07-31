# -*- coding: utf-8 -*-
"""
sku_formula_capability_generator.py

功能：
从三张 SKU 底层标签表读取标签，生成 SKU 级配方能力。

能力类型：
1. 单维配方能力：由单一维度标签直接推导
   例如：高动物蛋白配方能力、无谷碳水结构能力、复合纤维设计能力

2. 多维配方能力：由多个底层标签组合推导
   例如：高肉无谷配方能力、肠胃支持配方能力、低敏单一蛋白配方能力、皮毛支持配方能力

输出表：
sku_formula_capability_result

依赖：
pip install pymysql pandas sqlalchemy
"""

import json
import os
import pandas as pd
from sqlalchemy import create_engine, text
from typing import Dict, List, Any


# =========================================================
# 1. 数据库配置
# =========================================================

DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", os.getenv("DB_HOST", "127.0.0.1")),
    "port": int(os.getenv("MYSQL_PORT", os.getenv("DB_PORT", "3306"))),
    "user": os.getenv("MYSQL_USER", os.getenv("DB_USER", "root")),
    "password": os.getenv("MYSQL_PASSWORD", os.getenv("DB_PASSWORD", "")),
    "database": os.getenv("MYSQL_DATABASE", os.getenv("DB_NAME", "protein_feature_platform")),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
}


def get_engine():
    url = (
        f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
        f"?charset={DB_CONFIG['charset']}"
    )
    return create_engine(url)


# =========================================================
# 2. 读取三张底层标签表
# =========================================================

def load_base_tags(engine) -> pd.DataFrame:
    """
    读取三张底层标签表。

    你只需要保证最终合并后的字段为：
    sku_id
    sku_name
    tag_key
    tag_name
    tag_dimension
    tag_score
    evidence_text
    source_table
    """

    sql = """
    SELECT
        sku_id,
        sku_name,
        tag_key,
        tag_name,
        tag_dimension,
        COALESCE(tag_score, 1.0) AS tag_score,
        COALESCE(evidence_text, '') AS evidence_text,
        'sku_nutrition_structure_tags' AS source_table
    FROM sku_nutrition_structure_tags

    UNION ALL

    SELECT
        sku_id,
        sku_name,
        tag_key,
        tag_name,
        tag_dimension,
        COALESCE(tag_score, 1.0) AS tag_score,
        COALESCE(evidence_text, '') AS evidence_text,
        'sku_function_structure_tags' AS source_table
    FROM sku_function_structure_tags

    UNION ALL

    SELECT
        sku_id,
        sku_name,
        tag_key,
        tag_name,
        tag_dimension,
        COALESCE(tag_score, 1.0) AS tag_score,
        COALESCE(evidence_text, '') AS evidence_text,
        'sku_ingredient_path_tags' AS source_table
    FROM sku_ingredient_path_tags
    """

    df = pd.read_sql(sql, engine)

    df["tag_key"] = df["tag_key"].astype(str)
    df["tag_name"] = df["tag_name"].astype(str)
    df["tag_dimension"] = df["tag_dimension"].astype(str)
    df["tag_score"] = pd.to_numeric(df["tag_score"], errors="coerce").fillna(1.0)

    return df


# =========================================================
# 3. 配方能力规则
# =========================================================

"""
规则说明：

required_any:
- 满足其中任意一个 tag_key 即可

required_all:
- 必须全部满足

optional_any:
- 命中后加分

negative_any:
- 命中后扣分，或作为风险提示

capability_type:
- single_dimension 单维配方能力
- multi_dimension 多维配方能力
"""

FORMULA_CAPABILITY_RULES = [
    # =========================
    # 单维配方能力：蛋白
    # =========================
    {
        "capability_key": "high_animal_protein_capability",
        "capability_name": "高动物蛋白配方能力",
        "capability_type": "single_dimension",
        "required_any": [
            "high_animal_protein",
            "animal_protein_high",
            "fresh_meat_used",
            "meat_meal_used",
        ],
        "required_all": [],
        "optional_any": [
            "crude_protein_high",
            "fresh_meat_front",
            "meat_source_front",
        ],
        "negative_any": [
            "plant_protein_high",
        ],
        "base_score": 3.0,
        "description": "SKU 体现出以动物蛋白为核心的配方结构。"
    },
    {
        "capability_key": "fresh_meat_capability",
        "capability_name": "鲜肉/冻肉配方能力",
        "capability_type": "single_dimension",
        "required_any": [
            "fresh_meat_used",
            "frozen_meat_used",
            "meat_paste_used",
            "fresh_meat_front",
        ],
        "required_all": [],
        "optional_any": [
            "water_distribution_pressure",
            "high_animal_protein",
        ],
        "negative_any": [],
        "base_score": 3.0,
        "description": "SKU 使用鲜肉、冻肉或肉浆等湿态蛋白路径。"
    },
    {
        "capability_key": "raw_freeze_dried_meat_capability",
        "capability_name": "生骨肉/冻干鲜肉配方能力",
        "capability_type": "multi_dimension",
        "required_any": [
            "freeze_dried_used",
            "raw_bone_meat_used",
            "fresh_meat_freeze_dried_used",
        ],
        "required_all": [],
        "optional_any": [
            "fresh_meat_used",
            "high_animal_protein",
            "single_animal_protein",
            "multiple_animal_protein",
        ],
        "negative_any": [
            "plant_protein_high",
        ],
        "base_score": 4.5,
        "description": "SKU 以生骨肉、冻干或鲜肉冻干作为显性产品卖点，体现高肉源和高适口方向。"
    },
    {
        "capability_key": "meat_meal_stable_capability",
        "capability_name": "肉粉型稳定配方能力",
        "capability_type": "single_dimension",
        "required_any": [
            "meat_meal_used",
            "chicken_meal_used",
            "fish_meal_used",
            "meat_meal_front",
        ],
        "required_all": [],
        "optional_any": [
            "animal_protein_high",
        ],
        "negative_any": [],
        "base_score": 2.5,
        "description": "SKU 体现出以肉粉体系为基础的稳定膨化配方结构。"
    },
    {
        "capability_key": "hydrolyzed_low_allergy_capability",
        "capability_name": "水解/低敏蛋白配方能力",
        "capability_type": "single_dimension",
        "required_any": [
            "hydrolyzed_protein_used",
            "single_animal_protein",
            "limited_ingredient_protein",
        ],
        "required_all": [],
        "optional_any": [
            "plant_protein_low",
            "simple_protein_structure",
        ],
        "negative_any": [
            "multiple_animal_protein",
            "plant_protein_high",
        ],
        "base_score": 3.0,
        "description": "SKU 体现出低敏、单一蛋白或水解蛋白路径。"
    },
    {
        "capability_key": "multi_meat_high_palatability_capability",
        "capability_name": "多肉源高适口配方能力",
        "capability_type": "multi_dimension",
        "required_any": [
            "multiple_animal_protein",
            "multi_meat_source",
        ],
        "required_all": [],
        "optional_any": [
            "fresh_meat_used",
            "freeze_dried_used",
            "animal_fat_structure",
            "palatability_coating",
            "fish_oil_used",
        ],
        "negative_any": [
            "plant_protein_high",
        ],
        "base_score": 3.8,
        "description": "SKU 通过多动物蛋白、脂肪或冻干/喷涂结构增强适口性和肉源丰富度。"
    },

    # =========================
    # 单维配方能力：碳水
    # =========================
    {
        "capability_key": "grain_free_carb_capability",
        "capability_name": "无谷碳水结构能力",
        "capability_type": "single_dimension",
        "required_any": [
            "grain_free",
            "legume_starch_structure",
            "tuber_starch_structure",
            "pea_used",
            "potato_used",
        ],
        "required_all": [],
        "optional_any": [
            "grain_absent",
            "corn_absent",
            "wheat_absent",
        ],
        "negative_any": [
            "grain_starch_structure",
            "corn_used",
            "wheat_used",
            "rice_used",
        ],
        "base_score": 3.0,
        "description": "SKU 使用豆类、薯类等非谷物碳水路径。"
    },
    {
        "capability_key": "grain_starch_capability",
        "capability_name": "谷物型膨化结构能力",
        "capability_type": "single_dimension",
        "required_any": [
            "grain_starch_structure",
            "rice_used",
            "corn_used",
            "wheat_used",
            "oat_used",
        ],
        "required_all": [],
        "optional_any": [],
        "negative_any": [],
        "base_score": 2.5,
        "description": "SKU 体现出传统谷物淀粉支撑的膨化结构。"
    },

    # =========================
    # 单维配方能力：纤维/益生元/抗氧化/脂肪
    # =========================
    {
        "capability_key": "compound_fiber_capability",
        "capability_name": "复合纤维设计能力",
        "capability_type": "single_dimension",
        "required_any": [],
        "required_all": [
            "soluble_fiber_structure",
            "insoluble_fiber_structure",
        ],
        "optional_any": [
            "prebiotic_used",
            "gut_support_tag",
        ],
        "negative_any": [],
        "base_score": 3.5,
        "description": "SKU 同时具备可溶纤维和不可溶纤维结构。"
    },
    {
        "capability_key": "prebiotic_microbiome_capability",
        "capability_name": "益生元微生态配方能力",
        "capability_type": "single_dimension",
        "required_any": [
            "prebiotic_used",
            "fos_used",
            "mos_used",
            "inulin_used",
            "probiotic_used",
            "postbiotic_used",
        ],
        "required_all": [],
        "optional_any": [
            "compound_prebiotic",
            "soluble_fiber_structure",
        ],
        "negative_any": [],
        "base_score": 2.5,
        "description": "SKU 体现出益生元、益生菌或后生元相关微生态支持结构。"
    },
    {
        "capability_key": "gut_fiber_prebiotic_capability",
        "capability_name": "纤维益生元肠胃支持能力",
        "capability_type": "multi_dimension",
        "required_any": [
            "prebiotic_used",
            "compound_fiber",
            "soluble_fiber_structure",
        ],
        "required_all": [],
        "optional_any": [
            "insoluble_fiber_structure",
            "fat_moderate",
            "single_animal_protein",
            "plant_protein_low",
        ],
        "negative_any": [
            "fat_high",
            "plant_protein_high",
            "protein_complexity_high",
        ],
        "base_score": 3.2,
        "description": "SKU 以纤维、益生元或温和脂肪结构形成肠胃支持，不只是单一添加益生元。"
    },
    {
        "capability_key": "omega_skin_coat_capability",
        "capability_name": "脂肪适口与皮毛支持能力",
        "capability_type": "single_dimension",
        "required_any": [
            "omega3_support",
            "fish_oil_used",
            "flaxseed_used",
            "animal_fat_structure",
            "lecithin_used",
        ],
        "required_all": [],
        "optional_any": [
            "antioxidant_system",
            "vitamin_e_used",
        ],
        "negative_any": [],
        "base_score": 2.5,
        "description": "SKU 体现出脂肪酸、动物脂肪、鱼油或皮毛支持方向。"
    },
    {
        "capability_key": "antioxidant_system_capability",
        "capability_name": "抗氧化体系配置能力",
        "capability_type": "single_dimension",
        "required_any": [
            "antioxidant_system",
            "rosemary_extract_used",
            "vitamin_e_used",
            "tocopherol_used",
            "tea_polyphenol_used",
        ],
        "required_all": [],
        "optional_any": [
            "high_fat",
            "omega3_support",
        ],
        "negative_any": [],
        "base_score": 2.0,
        "description": "SKU 配置了天然抗氧化或维生素抗氧化体系。"
    },

    # =========================
    # 多维组合能力
    # =========================
    {
        "capability_key": "high_meat_grain_free_capability",
        "capability_name": "高肉无谷配方能力",
        "capability_type": "multi_dimension",
        "required_any": [],
        "required_all": [
            "high_animal_protein",
            "grain_free",
        ],
        "optional_any": [
            "fresh_meat_used",
            "meat_meal_used",
            "legume_starch_structure",
            "tuber_starch_structure",
        ],
        "negative_any": [
            "plant_protein_high",
        ],
        "base_score": 4.0,
        "description": "SKU 同时具备高动物蛋白和无谷碳水结构。"
    },
    {
        "capability_key": "high_meat_gut_support_capability",
        "capability_name": "高肉肠胃支持配方能力",
        "capability_type": "multi_dimension",
        "required_any": [
            "prebiotic_used",
            "compound_fiber",
            "soluble_fiber_structure",
        ],
        "required_all": [
            "high_animal_protein",
        ],
        "optional_any": [
            "compound_fiber",
            "soluble_fiber_structure",
            "insoluble_fiber_structure",
            "fat_moderate",
            "plant_protein_low",
        ],
        "negative_any": [
            "fat_high",
            "plant_protein_high",
            "protein_complexity_high",
        ],
        "base_score": 3.6,
        "min_support_tag_count": 2,
        "support_tag_pool": [
            "prebiotic_used",
            "compound_fiber",
            "soluble_fiber_structure",
            "insoluble_fiber_structure",
            "fat_moderate",
            "plant_protein_low",
        ],
        "description": "SKU 在高动物蛋白基础上具备至少两类肠胃支持证据，适合表达高肉肠胃支持方向。"
    },
    {
        "capability_key": "sensitive_stomach_capability",
        "capability_name": "肠胃友好配方能力",
        "capability_type": "multi_dimension",
        "required_any": [],
        "required_all": [
            "prebiotic_used",
        ],
        "optional_any": [
            "soluble_fiber_structure",
            "insoluble_fiber_structure",
            "compound_fiber",
            "single_animal_protein",
            "fat_moderate",
            "plant_protein_low",
        ],
        "negative_any": [
            "fat_high",
            "plant_protein_high",
            "protein_complexity_high",
        ],
        "base_score": 3.0,
        "description": "SKU 具备益生元、纤维、温和蛋白或适中脂肪等肠胃友好结构。"
    },
    {
        "capability_key": "low_allergy_limited_ingredient_capability",
        "capability_name": "低敏单一蛋白配方能力",
        "capability_type": "multi_dimension",
        "required_any": [],
        "required_all": [
            "single_animal_protein",
        ],
        "optional_any": [
            "hydrolyzed_protein_used",
            "grain_free",
            "simple_carb_structure",
            "plant_protein_low",
        ],
        "negative_any": [
            "multiple_animal_protein",
            "plant_protein_high",
            "protein_complexity_high",
        ],
        "base_score": 3.5,
        "description": "SKU 体现出单一蛋白、低复杂度和低敏设计方向。"
    },
    {
        "capability_key": "weight_control_capability",
        "capability_name": "控重/低脂配方能力",
        "capability_type": "multi_dimension",
        "required_any": [
            "fat_low",
            "fat_moderate",
        ],
        "required_all": [],
        "optional_any": [
            "fiber_high",
            "insoluble_fiber_structure",
            "protein_moderate",
            "starch_moderate",
        ],
        "negative_any": [
            "fat_high",
            "animal_fat_structure",
        ],
        "base_score": 2.5,
        "description": "SKU 体现出中低脂、纤维支持或控重方向。"
    },
    {
        "capability_key": "skin_coat_support_capability",
        "capability_name": "皮毛支持配方能力",
        "capability_type": "multi_dimension",
        "required_any": [],
        "required_all": [
            "omega3_support",
            "antioxidant_system",
        ],
        "optional_any": [
            "fish_oil_used",
            "lecithin_used",
            "vitamin_e_used",
            "flaxseed_used",
        ],
        "negative_any": [],
        "base_score": 3.5,
        "description": "SKU 同时具备 Omega-3 与抗氧化支持，适合皮肤毛发方向。"
    },
    {
        "capability_key": "fish_skin_coat_capability",
        "capability_name": "鱼肉/Omega皮毛支持能力",
        "capability_type": "multi_dimension",
        "required_any": [
            "fish_meat_source",
        ],
        "required_all": [],
        "optional_any": [
            "omega3_support",
            "fish_oil_used",
            "antioxidant_system",
            "vitamin_e_used",
            "fresh_meat_used",
            "high_animal_protein",
        ],
        "negative_any": [],
        "base_score": 3.8,
        "description": "SKU 以鱼肉、鱼油或 Omega-3 为核心，适合表达皮肤毛发和高适口方向。"
    },
]


# =========================================================
# 3.1 XMind 0515 收敛版配方画像规则
# =========================================================

# 旧版规则保留在上方作为历史参考；实际生成结果使用 0515 XMind 收敛版。
# 输出约定：
# - capability_type = profile：画像名称，最终只展示 Top 1
# - capability_type = level1_label：一级标签，最终最多展示 Top 2
# - capability_type = risk_shortcoming：风险短板，单独进入风险区
FORMULA_CAPABILITY_RULES = [
    # 动物蛋白结构画像：画像名称
    {
        "capability_key": "single_main_protein_clear_profile",
        "capability_name": "单一主蛋白清晰型",
        "capability_type": "profile",
        "required_any": ["single_animal_protein", "limited_ingredient_protein"],
        "required_all": [],
        "optional_any": ["plant_protein_low", "simple_protein_structure"],
        "negative_any": ["multiple_animal_protein", "plant_protein_high"],
        "base_score": 4.0,
        "description": "主蛋白来源清晰，适合低敏、敏感猫和配方归因。",
    },
    {
        "capability_key": "same_source_compound_protein_profile",
        "capability_name": "同源复合蛋白型",
        "capability_type": "profile",
        "required_any": ["fresh_meat_used", "meat_meal_used", "organ_involved", "same_species_multi_form"],
        "required_all": [],
        "optional_any": ["high_animal_protein", "animal_protein_high", "fresh_meat_front", "meat_source_front"],
        "negative_any": ["plant_protein_high"],
        "base_score": 4.0,
        "description": "同一动物来源下有多形态组合，兼顾原料表达和蛋白密度。",
    },
    {
        "capability_key": "multi_meat_compound_protein_profile",
        "capability_name": "多肉源复合蛋白型",
        "capability_type": "profile",
        "required_any": ["multiple_animal_protein", "multi_meat_source"],
        "required_all": [],
        "optional_any": ["fresh_meat_used", "meat_meal_used", "freeze_dried_used", "palatability_coating"],
        "negative_any": ["plant_protein_high"],
        "base_score": 4.4,
        "description": "肉源丰富、卖点和适口性强，但过敏归因和消化压力需观察。",
    },
    {
        "capability_key": "fish_skin_coat_profile",
        "capability_name": "鱼源皮毛支持型",
        "capability_type": "profile",
        "required_any": ["fish_meat_source", "fish_oil_used"],
        "required_all": [],
        "optional_any": ["omega3_support", "antioxidant_system", "vitamin_e_used", "high_animal_protein"],
        "negative_any": [],
        "base_score": 4.2,
        "description": "鱼源蛋白或鱼源补充明显，并联动 Omega/皮毛支持。",
    },
    {
        "capability_key": "hydrolyzed_low_allergy_profile",
        "capability_name": "水解/低敏蛋白型",
        "capability_type": "profile",
        "required_any": ["hydrolyzed_protein_used", "single_animal_protein", "limited_ingredient_protein"],
        "required_all": [],
        "optional_any": ["plant_protein_low", "simple_protein_structure"],
        "negative_any": ["multiple_animal_protein", "plant_protein_high"],
        "base_score": 4.0,
        "description": "水解蛋白或低敏蛋白结构明显，适合敏感猫方向。",
    },
    {
        "capability_key": "plant_protein_reinforced_profile",
        "capability_name": "植物蛋白补强型",
        "capability_type": "profile",
        "required_any": ["plant_protein_high"],
        "required_all": [],
        "optional_any": ["grain_starch_structure", "legume_starch_structure"],
        "negative_any": [],
        "base_score": 3.8,
        "description": "粗蛋白可能高，但动物蛋白主导性被植物蛋白削弱。",
    },

    # 碳水结构画像：画像名称
    {
        "capability_key": "grain_free_legume_tuber_carb_profile",
        "capability_name": "无谷豆薯碳水型",
        "capability_type": "profile",
        "required_any": [],
        "required_all": ["grain_free"],
        "optional_any": ["legume_starch_structure", "tuber_starch_structure"],
        "negative_any": ["grain_starch_structure"],
        "min_support_tag_count": 1,
        "support_tag_pool": ["legume_starch_structure", "tuber_starch_structure"],
        "base_score": 3.8,
        "description": "无谷表达强，但豆类/薯类可能带来消化或发酵压力。",
    },
    {
        "capability_key": "grain_free_low_starch_profile",
        "capability_name": "无谷低淀粉结构型",
        "capability_type": "profile",
        "required_any": ["grain_free"],
        "required_all": [],
        "optional_any": ["high_animal_protein", "fiber_high", "soluble_fiber_structure", "insoluble_fiber_structure"],
        "negative_any": ["legume_starch_structure", "tuber_starch_structure", "grain_starch_structure"],
        "base_score": 3.2,
        "description": "无谷且豆薯/精制淀粉压力低，碳水控制较好。",
    },
    {
        "capability_key": "grain_dominant_carb_profile",
        "capability_name": "谷物主导碳水型",
        "capability_type": "profile",
        "required_any": ["grain_starch_structure", "rice_used", "corn_used", "wheat_used", "oat_used"],
        "required_all": [],
        "optional_any": [],
        "negative_any": [],
        "base_score": 3.2,
        "description": "传统谷物结构，需关注消化适配。",
    },
    {
        "capability_key": "legume_dominant_carb_profile",
        "capability_name": "豆类碳水主导型",
        "capability_type": "profile",
        "required_any": ["legume_starch_structure", "pea_used"],
        "required_all": [],
        "optional_any": ["grain_free"],
        "negative_any": [],
        "base_score": 3.2,
        "description": "豆类靠前，可能增加软便或发酵压力。",
    },
    {
        "capability_key": "tuber_dominant_carb_profile",
        "capability_name": "薯类淀粉主导型",
        "capability_type": "profile",
        "required_any": ["tuber_starch_structure", "potato_used"],
        "required_all": [],
        "optional_any": ["grain_free"],
        "negative_any": [],
        "base_score": 3.2,
        "description": "薯类/木薯类淀粉负担需观察。",
    },

    # 脂肪、肠胃、抗氧化画像：画像名称
    {
        "capability_key": "omega3_support_profile",
        "capability_name": "Omega-3支持型",
        "capability_type": "profile",
        "required_any": ["omega3_support", "fish_oil_used", "flaxseed_used"],
        "required_all": [],
        "optional_any": ["antioxidant_system", "vitamin_e_used"],
        "negative_any": [],
        "base_score": 3.5,
        "description": "鱼油、亚麻籽油、亚麻籽等提供脂肪调节支持。",
    },
    {
        "capability_key": "animal_fat_palatability_profile",
        "capability_name": "动物脂肪适口增强型",
        "capability_type": "profile",
        "required_any": ["animal_fat_structure", "palatability_coating"],
        "required_all": [],
        "optional_any": ["fat_moderate", "fat_high"],
        "negative_any": [],
        "base_score": 3.2,
        "description": "动物脂肪明显，适口性强但脂肪压力需观察。",
    },
    {
        "capability_key": "gut_fiber_support_profile",
        "capability_name": "肠胃纤维支持型",
        "capability_type": "profile",
        "required_any": ["soluble_fiber_structure", "insoluble_fiber_structure", "fiber_high"],
        "required_all": [],
        "optional_any": ["prebiotic_used", "fat_moderate"],
        "negative_any": ["fat_high", "plant_protein_high"],
        "base_score": 3.4,
        "description": "纤维结构对便便成形有支持。",
    },
    {
        "capability_key": "prebiotic_microbiome_profile",
        "capability_name": "益生元微生态支持型",
        "capability_type": "profile",
        "required_any": ["prebiotic_used", "fos_used", "mos_used", "inulin_used", "probiotic_used", "postbiotic_used"],
        "required_all": [],
        "optional_any": ["soluble_fiber_structure", "compound_prebiotic"],
        "negative_any": [],
        "base_score": 3.4,
        "description": "益生元/供菌底物对菌群代谢有支持。",
    },
    {
        "capability_key": "formed_stool_fiber_profile",
        "capability_name": "成形纤维强化型",
        "capability_type": "profile",
        "required_any": [],
        "required_all": ["soluble_fiber_structure", "insoluble_fiber_structure"],
        "optional_any": ["prebiotic_used", "fiber_high"],
        "negative_any": [],
        "base_score": 4.0,
        "description": "可溶+不可溶纤维结构相对完整。",
    },
    {
        "capability_key": "mild_gut_support_profile",
        "capability_name": "温和肠胃支持型",
        "capability_type": "profile",
        "required_any": ["prebiotic_used", "soluble_fiber_structure", "insoluble_fiber_structure", "fat_moderate"],
        "required_all": [],
        "optional_any": ["single_animal_protein", "plant_protein_low"],
        "negative_any": ["fat_high", "plant_protein_high", "protein_complexity_high"],
        "base_score": 3.2,
        "description": "低刺激碳水、纤维、益生元组合较好。",
    },
    {
        "capability_key": "antioxidant_auxiliary_profile",
        "capability_name": "抗氧化辅助支持型",
        "capability_type": "profile",
        "required_any": ["antioxidant_system", "rosemary_extract_used", "vitamin_e_used", "tocopherol_used", "tea_polyphenol_used"],
        "required_all": [],
        "optional_any": ["omega3_support", "fish_oil_used"],
        "negative_any": [],
        "base_score": 3.0,
        "description": "存在维E、迷迭香、莓果、多酚等基础支持。",
    },
    {
        "capability_key": "inflammation_buffer_profile",
        "capability_name": "炎症缓冲支持型",
        "capability_type": "profile",
        "required_any": [],
        "required_all": ["omega3_support", "antioxidant_system"],
        "optional_any": ["prebiotic_used", "fish_oil_used", "vitamin_e_used"],
        "negative_any": [],
        "base_score": 4.0,
        "description": "Omega-3 + 抗氧化 + 微生态支持较完整。",
    },

    # 一级标签：动物蛋白结构
    {
        "capability_key": "meat_complexity_single_label",
        "capability_name": "肉源复杂度｜单一肉源",
        "capability_type": "level1_label",
        "required_any": ["single_animal_protein"],
        "required_all": [],
        "optional_any": [],
        "negative_any": [],
        "base_score": 3.0,
        "description": "蛋白归因清晰，低敏筛选友好。",
    },
    {
        "capability_key": "meat_complexity_multi_label",
        "capability_name": "肉源复杂度｜跨类/多源复合",
        "capability_type": "level1_label",
        "required_any": ["multiple_animal_protein", "multi_meat_source"],
        "required_all": [],
        "optional_any": ["fish_meat_source", "fresh_meat_used", "meat_meal_used"],
        "negative_any": [],
        "base_score": 3.4,
        "description": "禽、鱼、红肉等多类混合，适口强但归因复杂。",
    },
    {
        "capability_key": "main_protein_form_fresh_meat_label",
        "capability_name": "主蛋白形态｜鲜肉/冻肉参与",
        "capability_type": "level1_label",
        "required_any": ["fresh_meat_used", "frozen_meat_used", "meat_paste_used"],
        "required_all": [],
        "optional_any": ["high_animal_protein"],
        "negative_any": [],
        "base_score": 3.0,
        "description": "鲜肉或冻肉参与明显，原料表达较强。",
    },
    {
        "capability_key": "main_protein_form_meat_meal_label",
        "capability_name": "主蛋白形态｜肉粉主导",
        "capability_type": "level1_label",
        "required_any": ["meat_meal_used", "chicken_meal_used", "fish_meal_used"],
        "required_all": [],
        "optional_any": ["animal_protein_high"],
        "negative_any": [],
        "base_score": 2.8,
        "description": "干物质蛋白密度稳定，偏营养效率。",
    },
    {
        "capability_key": "secondary_protein_supplement_label",
        "capability_name": "次蛋白补充方式｜风味/功能补充",
        "capability_type": "level1_label",
        "required_any": ["organ_involved", "hydrolyzed_protein_used", "fish_meat_source", "freeze_dried_used", "raw_bone_meat_used"],
        "required_all": [],
        "optional_any": ["palatability_coating", "omega3_support"],
        "negative_any": [],
        "base_score": 2.8,
        "description": "通过内脏、水解蛋白、鱼源或冻干等增强适口或功能表达。",
    },
    {
        "capability_key": "plant_protein_reinforcement_label",
        "capability_name": "植物蛋白补充｜明显植物蛋白补强",
        "capability_type": "level1_label",
        "required_any": ["plant_protein_high"],
        "required_all": [],
        "optional_any": [],
        "negative_any": [],
        "base_score": 3.6,
        "description": "豌豆蛋白、大豆蛋白、玉米蛋白粉等参与，抬高粗蛋白。",
    },

    # 一级标签：碳水、脂肪、肠胃、抗氧化
    {
        "capability_key": "grain_free_or_grain_label",
        "capability_name": "无谷/有谷结构｜无谷结构",
        "capability_type": "level1_label",
        "required_any": ["grain_free"],
        "required_all": [],
        "optional_any": ["legume_starch_structure", "tuber_starch_structure"],
        "negative_any": [],
        "base_score": 2.8,
        "description": "不含谷物，但不等于低碳水。",
    },
    {
        "capability_key": "carb_source_legume_tuber_label",
        "capability_name": "碳水来源类型｜豆薯型",
        "capability_type": "level1_label",
        "required_any": ["legume_starch_structure", "tuber_starch_structure"],
        "required_all": [],
        "optional_any": ["grain_free"],
        "negative_any": [],
        "base_score": 3.0,
        "description": "豆类或薯类参与，需看具体顺位和消化负担。",
    },
    {
        "capability_key": "fat_source_fish_oil_label",
        "capability_name": "脂肪来源结构｜鱼油支持",
        "capability_type": "level1_label",
        "required_any": ["fish_oil_used", "omega3_support"],
        "required_all": [],
        "optional_any": ["antioxidant_system"],
        "negative_any": [],
        "base_score": 3.0,
        "description": "Omega-3 直接来源，支持皮毛和炎症缓冲。",
    },
    {
        "capability_key": "fat_source_animal_fat_label",
        "capability_name": "脂肪来源结构｜动物脂肪来源集中",
        "capability_type": "level1_label",
        "required_any": ["animal_fat_structure", "fat_high"],
        "required_all": [],
        "optional_any": ["palatability_coating"],
        "negative_any": [],
        "base_score": 2.8,
        "description": "适口强，但皮脂/软便风险需结合脂肪水平判断。",
    },
    {
        "capability_key": "stool_forming_support_label",
        "capability_name": "便便成形支持｜纤维成形支持",
        "capability_type": "level1_label",
        "required_any": ["soluble_fiber_structure", "insoluble_fiber_structure", "fiber_high"],
        "required_all": [],
        "optional_any": ["prebiotic_used"],
        "negative_any": [],
        "base_score": 3.0,
        "description": "帮助粪便锁水、成形和结构稳定。",
    },
    {
        "capability_key": "microbiome_metabolism_label",
        "capability_name": "菌群代谢支持｜益生元/供菌底物支持",
        "capability_type": "level1_label",
        "required_any": ["prebiotic_used", "fos_used", "mos_used", "inulin_used", "probiotic_used"],
        "required_all": [],
        "optional_any": ["soluble_fiber_structure"],
        "negative_any": [],
        "base_score": 3.0,
        "description": "支持有益菌和肠道稳定。",
    },
    {
        "capability_key": "antioxidant_source_label",
        "capability_name": "抗氧化来源｜维生素/植物抗氧化支持",
        "capability_type": "level1_label",
        "required_any": ["antioxidant_system", "vitamin_e_used", "rosemary_extract_used", "tocopherol_used", "tea_polyphenol_used"],
        "required_all": [],
        "optional_any": [],
        "negative_any": [],
        "base_score": 2.8,
        "description": "维E、迷迭香、植物多酚等基础抗氧化支持。",
    },

    # 风险短板
    {
        "capability_key": "legume_tuber_carb_pressure_risk",
        "capability_name": "风险短板｜豆薯碳水压力偏高",
        "capability_type": "risk_shortcoming",
        "required_any": ["legume_starch_structure", "tuber_starch_structure"],
        "required_all": [],
        "optional_any": ["grain_free", "plant_protein_high"],
        "negative_any": [],
        "base_score": 2.6,
        "description": "豆类/薯类靠前时，软便或发酵压力需观察。",
    },
    {
        "capability_key": "plant_protein_dominance_risk",
        "capability_name": "风险短板｜植物蛋白补强明显",
        "capability_type": "risk_shortcoming",
        "required_any": ["plant_protein_high"],
        "required_all": [],
        "optional_any": ["legume_starch_structure", "grain_starch_structure"],
        "negative_any": [],
        "base_score": 2.8,
        "description": "动物蛋白纯度下降，需关注消化和氨基酸结构。",
    },
    {
        "capability_key": "fat_pressure_risk",
        "capability_name": "风险短板｜脂肪/皮脂压力偏高",
        "capability_type": "risk_shortcoming",
        "required_any": ["fat_high", "animal_fat_structure"],
        "required_all": [],
        "optional_any": ["palatability_coating"],
        "negative_any": [],
        "base_score": 2.6,
        "description": "动物脂肪或高脂结构明显时，皮脂和软便压力需观察。",
    },
    {
        "capability_key": "antioxidant_buffer_insufficient_risk",
        "capability_name": "风险短板｜抗氧化缓冲不足",
        "capability_type": "risk_shortcoming",
        "required_any": ["fat_high", "animal_fat_structure", "omega6_pressure_high"],
        "required_all": [],
        "optional_any": [],
        "negative_any": ["antioxidant_system", "omega3_support"],
        "base_score": 2.0,
        "description": "高脂/皮肤/泪痕场景下保护不足，需要与脂肪、Omega 和症状链路联动判断。",
    },
]


# =========================================================
# 4. 单 SKU 能力计算逻辑
# =========================================================

def sku_tag_dict(sku_df: pd.DataFrame) -> Dict[str, Dict[str, Any]]:
    """
    把一个 SKU 的底层标签转成 dict。
    key = tag_key
    value = 标签信息
    """
    tag_map = {}

    for _, row in sku_df.iterrows():
        tag_key = row["tag_key"]
        tag_map[tag_key] = {
            "tag_name": row["tag_name"],
            "tag_dimension": row["tag_dimension"],
            "tag_score": float(row["tag_score"]),
            "evidence_text": row.get("evidence_text", ""),
            "source_table": row.get("source_table", ""),
        }

    return tag_map


def has_any(tag_map: Dict[str, Dict[str, Any]], required_any: List[str]) -> bool:
    if not required_any:
        return True
    return any(tag in tag_map for tag in required_any)


def has_all(tag_map: Dict[str, Dict[str, Any]], required_all: List[str]) -> bool:
    if not required_all:
        return True
    return all(tag in tag_map for tag in required_all)


def matched_tags(tag_map: Dict[str, Dict[str, Any]], candidate_tags: List[str]) -> List[str]:
    return [tag for tag in candidate_tags if tag in tag_map]


def calc_capability_for_sku(
    sku_id: str,
    sku_name: str,
    tag_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    根据底层标签生成该 SKU 的配方能力。
    """

    results = []

    for rule in FORMULA_CAPABILITY_RULES:
        required_any = rule.get("required_any", [])
        required_all = rule.get("required_all", [])
        optional_any = rule.get("optional_any", [])
        negative_any = rule.get("negative_any", [])

        if not has_any(tag_map, required_any):
            continue

        if not has_all(tag_map, required_all):
            continue

        hit_required_any = matched_tags(tag_map, required_any)
        hit_required_all = matched_tags(tag_map, required_all)
        hit_optional = matched_tags(tag_map, optional_any)
        hit_negative = matched_tags(tag_map, negative_any)

        min_support_tag_count = int(rule.get("min_support_tag_count", 0) or 0)
        if min_support_tag_count:
            support_hits = matched_tags(tag_map, rule.get("support_tag_pool", []))
            if len(support_hits) < min_support_tag_count:
                continue

        evidence_tags = list(dict.fromkeys(hit_required_any + hit_required_all + hit_optional))

        if not evidence_tags:
            continue

        # 基础分
        score = float(rule["base_score"])

        # 必选标签得分
        for tag in hit_required_any + hit_required_all:
            score += float(tag_map[tag]["tag_score"]) * 1.0

        # 可选标签加分
        for tag in hit_optional:
            score += float(tag_map[tag]["tag_score"]) * 0.5

        # 负向标签扣分
        for tag in hit_negative:
            score -= float(tag_map[tag]["tag_score"]) * 0.8

        score = max(score, 0)
        score = round(score, 3)

        if score >= 7:
            level = "强"
        elif score >= 4:
            level = "中"
        elif score > 0:
            level = "弱"
        else:
            level = "无"

        if level == "无":
            continue

        evidence_detail = []
        for tag in evidence_tags:
            evidence_detail.append({
                "tag_key": tag,
                "tag_name": tag_map[tag]["tag_name"],
                "tag_dimension": tag_map[tag]["tag_dimension"],
                "tag_score": tag_map[tag]["tag_score"],
                "evidence_text": tag_map[tag]["evidence_text"],
            })

        negative_detail = []
        for tag in hit_negative:
            negative_detail.append({
                "tag_key": tag,
                "tag_name": tag_map[tag]["tag_name"],
                "tag_dimension": tag_map[tag]["tag_dimension"],
                "tag_score": tag_map[tag]["tag_score"],
                "evidence_text": tag_map[tag]["evidence_text"],
            })

        results.append({
            "sku_id": sku_id,
            "sku_name": sku_name,
            "capability_key": rule["capability_key"],
            "capability_name": rule["capability_name"],
            "capability_type": rule["capability_type"],
            "capability_score": score,
            "capability_level": level,
            "description": rule["description"],
            "evidence_tags": evidence_tags,
            "evidence_detail": evidence_detail,
            "negative_tags": hit_negative,
            "negative_detail": negative_detail,
        })

    return results


def choose_main_and_assist_capabilities(capabilities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    按 0515 XMind 收敛规则筛选前台展示：
    - 画像名称：Top 1，仍使用“主配方能力”角色，兼容旧页面
    - 一级标签：Top 2，使用“辅助配方能力”角色，兼容旧页面
    - 风险短板：Top 2，单独标记为“风险短板”
    - 其他命中结果进入“补充配方能力”
    """

    if not capabilities:
        return []

    type_weight = {"profile": 2.0, "level1_label": 1.2, "risk_shortcoming": 1.0}
    profile_priority = {
        "fish_skin_coat_profile": 4.2,
        "multi_meat_compound_protein_profile": 4.0,
        "same_source_compound_protein_profile": 3.8,
        "single_main_protein_clear_profile": 3.6,
        "hydrolyzed_low_allergy_profile": 3.6,
        "formed_stool_fiber_profile": 3.2,
        "mild_gut_support_profile": 3.0,
        "prebiotic_microbiome_profile": 2.8,
        "gut_fiber_support_profile": 2.6,
        "inflammation_buffer_profile": 2.4,
        "grain_free_legume_tuber_carb_profile": 2.2,
        "omega3_support_profile": 2.0,
        "animal_fat_palatability_profile": 1.8,
        "antioxidant_auxiliary_profile": 1.6,
        "plant_protein_reinforced_profile": 1.2,
        "grain_dominant_carb_profile": 1.0,
        "legume_dominant_carb_profile": 1.0,
        "tuber_dominant_carb_profile": 1.0,
    }

    for item in capabilities:
        key = item.get("capability_key")
        item["_sort_score"] = (
            item["capability_score"] * type_weight.get(item.get("capability_type"), 1.0)
            + profile_priority.get(key, 0.0)
        )

    capabilities_sorted = sorted(
        capabilities,
        key=lambda x: x["_sort_score"],
        reverse=True
    )

    profile_items = [item for item in capabilities_sorted if item.get("capability_type") == "profile"]
    label_items = [item for item in capabilities_sorted if item.get("capability_type") == "level1_label"]
    risk_items = [item for item in capabilities_sorted if item.get("capability_type") == "risk_shortcoming"]

    main_key = profile_items[0].get("capability_key") if profile_items else None
    label_keys = {item.get("capability_key") for item in label_items[:2]}
    risk_keys = {item.get("capability_key") for item in risk_items[:2]}

    for item in capabilities_sorted:
        key = item.get("capability_key")
        if key == main_key:
            display_role = "主配方能力"
        elif key in label_keys:
            display_role = "辅助配方能力"
        elif key in risk_keys:
            display_role = "风险短板"
        else:
            display_role = "补充配方能力"

        item["display_role"] = display_role
        item.pop("_sort_score", None)

    return capabilities_sorted


# =========================================================
# 5. 批量生成 SKU 配方能力
# =========================================================

def generate_sku_formula_capabilities(base_tag_df: pd.DataFrame) -> pd.DataFrame:
    all_results = []

    group_cols = ["sku_id", "sku_name"]

    for (sku_id, sku_name), sku_df in base_tag_df.groupby(group_cols):
        tag_map = sku_tag_dict(sku_df)

        capabilities = calc_capability_for_sku(
            sku_id=sku_id,
            sku_name=sku_name,
            tag_map=tag_map,
        )

        capabilities = choose_main_and_assist_capabilities(capabilities)

        for cap in capabilities:
            all_results.append({
                "sku_id": cap["sku_id"],
                "sku_name": cap["sku_name"],
                "capability_key": cap["capability_key"],
                "capability_name": cap["capability_name"],
                "capability_type": cap["capability_type"],
                "display_role": cap["display_role"],
                "capability_score": cap["capability_score"],
                "capability_level": cap["capability_level"],
                "description": cap["description"],
                "evidence_tags_json": json.dumps(cap["evidence_tags"], ensure_ascii=False),
                "evidence_detail_json": json.dumps(cap["evidence_detail"], ensure_ascii=False),
                "negative_tags_json": json.dumps(cap["negative_tags"], ensure_ascii=False),
                "negative_detail_json": json.dumps(cap["negative_detail"], ensure_ascii=False),
            })

    return pd.DataFrame(all_results)


# =========================================================
# 6. 建表与写入结果
# =========================================================

def create_result_table(engine):
    sql = """
    CREATE TABLE IF NOT EXISTS sku_formula_capability_result (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        sku_id VARCHAR(100) NOT NULL,
        sku_name VARCHAR(255),
        capability_key VARCHAR(100) NOT NULL,
        capability_name VARCHAR(100) NOT NULL,
        capability_type VARCHAR(50),
        display_role VARCHAR(50),
        capability_score DECIMAL(10,3),
        capability_level VARCHAR(20),
        description TEXT,
        evidence_tags_json JSON,
        evidence_detail_json JSON,
        negative_tags_json JSON,
        negative_detail_json JSON,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        KEY idx_sku_id (sku_id),
        KEY idx_capability_key (capability_key),
        KEY idx_display_role (display_role)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    with engine.begin() as conn:
        conn.execute(text(sql))


def write_result(engine, result_df: pd.DataFrame, truncate: bool = True):
    create_result_table(engine)

    with engine.begin() as conn:
        if truncate:
            conn.execute(text("TRUNCATE TABLE sku_formula_capability_result"))

    if result_df.empty:
        print("没有生成任何配方能力结果。")
        return

    result_df.to_sql(
        "sku_formula_capability_result",
        engine,
        if_exists="append",
        index=False,
        chunksize=1000,
    )


# =========================================================
# 7. 本地演示数据：如果你暂时不连数据库，可以先跑这个
# =========================================================

def build_demo_base_tags() -> pd.DataFrame:
    data = [
        # SKU001：高肉无谷复合纤维
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "high_animal_protein",
            "tag_name": "高动物蛋白",
            "tag_dimension": "protein",
            "tag_score": 2.0,
            "evidence_text": "鲜鸡肉、鸡肉粉靠前",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "fresh_meat_used",
            "tag_name": "鲜肉/冻肉使用",
            "tag_dimension": "protein",
            "tag_score": 2.0,
            "evidence_text": "鲜鸡肉",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "grain_free",
            "tag_name": "无谷结构",
            "tag_dimension": "carb",
            "tag_score": 2.0,
            "evidence_text": "未识别谷物，使用豌豆、马铃薯",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "legume_starch_structure",
            "tag_name": "豆类碳水结构",
            "tag_dimension": "carb",
            "tag_score": 1.5,
            "evidence_text": "豌豆",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "soluble_fiber_structure",
            "tag_name": "可溶纤维结构",
            "tag_dimension": "fiber",
            "tag_score": 1.2,
            "evidence_text": "菊粉",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "insoluble_fiber_structure",
            "tag_name": "不可溶纤维结构",
            "tag_dimension": "fiber",
            "tag_score": 1.2,
            "evidence_text": "豌豆纤维",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "prebiotic_used",
            "tag_name": "益生元添加",
            "tag_dimension": "prebiotic",
            "tag_score": 1.0,
            "evidence_text": "菊粉",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "omega3_support",
            "tag_name": "Omega-3支持",
            "tag_dimension": "fat",
            "tag_score": 1.0,
            "evidence_text": "鱼油",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU001",
            "sku_name": "高肉无谷鲜鸡猫粮",
            "tag_key": "antioxidant_system",
            "tag_name": "抗氧化体系",
            "tag_dimension": "antioxidant",
            "tag_score": 1.0,
            "evidence_text": "迷迭香、维生素E",
            "source_table": "demo",
        },

        # SKU002：低敏单一蛋白
        {
            "sku_id": "SKU002",
            "sku_name": "低敏单一鸭肉猫粮",
            "tag_key": "single_animal_protein",
            "tag_name": "单一动物蛋白",
            "tag_dimension": "protein",
            "tag_score": 2.0,
            "evidence_text": "单一鸭肉来源",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU002",
            "sku_name": "低敏单一鸭肉猫粮",
            "tag_key": "grain_free",
            "tag_name": "无谷结构",
            "tag_dimension": "carb",
            "tag_score": 1.5,
            "evidence_text": "马铃薯作为碳水来源",
            "source_table": "demo",
        },
        {
            "sku_id": "SKU002",
            "sku_name": "低敏单一鸭肉猫粮",
            "tag_key": "plant_protein_low",
            "tag_name": "植物蛋白低",
            "tag_dimension": "protein",
            "tag_score": 1.0,
            "evidence_text": "未识别明显植物蛋白补强",
            "source_table": "demo",
        },
    ]

    return pd.DataFrame(data)


# =========================================================
# 8. 主程序
# =========================================================

def main(use_database: bool = False):
    if use_database:
        engine = get_engine()
        base_tag_df = load_base_tags(engine)
    else:
        engine = None
        base_tag_df = build_demo_base_tags()

    result_df = generate_sku_formula_capabilities(base_tag_df)

    print("\n====== SKU 配方能力结果预览 ======")
    if result_df.empty:
        print("未生成结果。请检查 tag_key 是否与规则表匹配。")
    else:
        print(result_df[
            [
                "sku_id",
                "sku_name",
                "capability_name",
                "capability_type",
                "display_role",
                "capability_score",
                "capability_level",
            ]
        ].to_string(index=False))

    # 本地导出 CSV
    result_df.to_csv(
        "sku_formula_capability_result.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print("\n已导出：sku_formula_capability_result.csv")

    # 写入数据库
    if use_database and engine is not None:
        write_result(engine, result_df, truncate=True)
        print("已写入数据库表：sku_formula_capability_result")


if __name__ == "__main__":
    # 第一次建议先用 False 跑 demo
    # 确认逻辑没问题后，改成 True 读取你的本地 MySQL 三张标签表
    main(use_database=False)
