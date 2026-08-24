# -*- coding: utf-8 -*-
"""
将猫粮原料分类到 11 个工艺结构模块。

用法：
  # 只导出原料 CSV，不调 LLM
  python scripts/classify_ingredients_to_process_modules.py --export-only

  # 种子写入 + LLM 批量分类 + 写入表
  python scripts/classify_ingredients_to_process_modules.py

  # 仅对未分类原料调用 LLM（增量）
  python scripts/classify_ingredients_to_process_modules.py --incremental

环境变量：
  DASHSCOPE_API_KEY  - 通义千问 API Key
  QWEN_MODEL         - 模型名称 (默认 qwen-plus)
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pymysql
from openai import OpenAI


# =========================
# 配置
# =========================

DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "protein_feature_platform"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

LABEL_DB = os.getenv("LABEL_SOURCE_DATABASE", "csv_labeling")
OUTPUT_TABLE = "catfood_ingredient_process_module"
REVIEW_CSV = os.path.join(os.path.dirname(__file__), "..", "var", "ingredient_classification_review.csv")
BATCH_SIZE = 15          # 每次 LLM 调用处理的原料数
MAX_RETRIES = 3
LLM_TIMEOUT = 60
RATE_LIMIT_INTERVAL = 1.5  # 秒，两次 LLM 调用之间的最小间隔


MODULE_COLUMNS = [
    "moisture_drying",
    "animal_protein_powder",
    "plant_protein",
    "starch_extrusion",
    "oil_spray",
    "oxidation_sensitivity",
    "palatability_process",
    "fermentation_substrate",
    "fiber_structure",
    "water_binding_gel",
    "mineral_powder",
]

# =========================
# 种子规则 (来自 sku_process_feature.py PROCESS_RULES)
# =========================

SEED_RULES = [
    {
        "keywords": ["冻鸡肉", "鲜鸡肉", "冻鸭肉", "鲜鸭肉", "冻牛肉", "鲜牛肉", "鲜肉", "冻肉",
                      "鲜羊肉", "冻羊肉", "鲜兔肉", "冻兔肉", "鲜火鸡肉", "冻火鸡肉"],
        "modules": {"moisture_drying": 1},
        "category": "动物蛋白-鲜肉",
        "base_score": 2.0,
    },
    {
        "keywords": ["鸡肉粉", "鸭肉粉", "鱼粉", "牛肉粉", "肉粉", "羊肉粉", "兔肉粉",
                      "火鸡肉粉", "鸡肉干粉", "鸭肉干粉"],
        "modules": {"animal_protein_powder": 1},
        "category": "动物蛋白-肉粉",
        "base_score": 1.8,
    },
    {
        "keywords": ["玉米蛋白粉", "小麦蛋白粉", "豌豆蛋白", "马铃薯蛋白", "大豆粕", "豆粕",
                      "大豆蛋白", "豌豆蛋白粉", "玉米蛋白"],
        "modules": {"plant_protein": 1},
        "category": "植物蛋白",
        "base_score": 1.8,
    },
    {
        "keywords": ["小麦", "大米", "玉米", "糙米", "燕麦", "木薯", "马铃薯", "红薯", "豌豆粉",
                      "高粱", "紫薯", "甘薯", "木薯粉", "马铃薯粉", "大米粉", "糙米粉",
                      "甘薯粉", "红薯粉", "玉米粉", "燕麦粉"],
        "modules": {"starch_extrusion": 1},
        "category": "碳水/淀粉",
        "base_score": 1.5,
    },
    {
        "keywords": ["鸡油", "鸭油", "牛油", "动物脂肪", "禽脂", "猪油"],
        "modules": {"oil_spray": 1},
        "category": "油脂-动物",
        "base_score": 1.8,
    },
    {
        "keywords": ["鱼油", "亚麻籽油", "藻油", "三文鱼油", "鳀鱼油", "磷虾油", "深海鱼油"],
        "modules": {"oxidation_sensitivity": 1},
        "category": "油脂-功能",
        "base_score": 2.0,
    },
    {
        "keywords": ["鸡肝水解粉", "水解鸡肝", "水解蛋白", "酵母抽提物", "酵母提取物",
                      "诱食剂", "肝粉", "鸡肝粉", "酶解鸡肝", "风味增强剂"],
        "modules": {"palatability_process": 1},
        "category": "适口增强",
        "base_score": 1.7,
    },
    {
        "keywords": ["菊糖", "低聚果糖", "低聚半乳糖", "甘露寡糖", "果寡糖", "菊苣根粉",
                      "菊苣根", "低聚异麦芽糖"],
        "modules": {"fermentation_substrate": 1},
        "category": "益生元",
        "base_score": 1.6,
    },
    {
        "keywords": ["甜菜粕", "车前子", "苜蓿", "纤维素", "南瓜", "苹果纤维",
                      "甜菜浆", "苜蓿草颗粒", "菊苣纤维"],
        "modules": {"fiber_structure": 1},
        "category": "纤维",
        "base_score": 1.5,
    },
    {
        "keywords": ["瓜尔胶", "黄原胶", "魔芋粉", "果胶", "卡拉胶"],
        "modules": {"water_binding_gel": 1},
        "category": "胶质",
        "base_score": 2.0,
    },
    {
        "keywords": ["矿物质", "碳酸钙", "磷酸氢钙", "氯化钠", "硫酸锌", "硫酸铜",
                      "磷酸钙", "食盐", "氯化钾", "碘酸钙", "亚硒酸钠", "硫酸锰",
                      "硫酸亚铁", "氧化锌", "碘化钾"],
        "modules": {"mineral_powder": 1},
        "category": "矿物质",
        "base_score": 1.0,
    },
]


# =========================
# LLM 分类 Prompt
# =========================

MODULE_DESCRIPTIONS = {
    "moisture_drying": "高含水动物基质：鲜/冻/冷藏状态的肉类（含大量水分，需要干燥工艺）",
    "animal_protein_powder": "动物蛋白干粉基质：肉粉、鱼粉、脱水肉等粉状动物蛋白",
    "plant_protein": "植物蛋白补强：豆粕、豌豆蛋白、玉米蛋白等植物来源蛋白浓缩物",
    "starch_extrusion": "淀粉膨化支撑：谷物、薯类、豆类等含淀粉原料（影响膨化成型）",
    "oil_spray": "后喷涂油脂结构：动物油脂（鸡油、鸭油、牛油等，用于后喷涂）",
    "oxidation_sensitivity": "氧化敏感脂肪：鱼油、亚麻籽油、藻油等富含不饱和脂肪酸的油脂",
    "palatability_process": "风味适口增强：水解蛋白、酵母提取物、肝粉、诱食剂等",
    "fermentation_substrate": "益生元发酵底物：低聚糖、菊糖等可被肠道菌群发酵的底物",
    "fiber_structure": "纤维结构支持：甜菜粕、车前子、苜蓿等膳食纤维来源",
    "water_binding_gel": "高吸水胶质：瓜尔胶、黄原胶、魔芋粉、果胶等吸水增稠剂",
    "mineral_powder": "矿物粉体结构：碳酸钙、磷酸氢钙等矿物质预混料",
}

SYSTEM_PROMPT = """你是猫粮原料工艺分类专家。你需要判断每个原料属于哪些工艺结构模块（可多选，也可都不属于）。

模块定义：
""" + "\n".join(f"- {k}: {v}" for k, v in MODULE_DESCRIPTIONS.items()) + """

原料大类(category)可选值：动物蛋白-鲜肉, 动物蛋白-肉粉, 动物蛋白-内脏, 动物蛋白-其他, 植物蛋白, 碳水/淀粉, 油脂-动物, 油脂-植物, 油脂-功能, 纤维, 益生元, 胶质, 矿物质, 维生素, 氨基酸, 抗氧化剂, 功能添加, 调味/诱食, 其他

输出格式（严格 JSON 数组，每个元素对应一个原料）：
[{"name":"原料名","category":"大类","modules":["module_key1","module_key2"]},...]

如果原料不属于任何工艺模块，modules 输出空数组 []。
只输出 JSON，不要任何额外文字。"""


def build_batch_prompt(ingredients: List[str]) -> str:
    return f"请对以下 {len(ingredients)} 个猫粮原料进行工艺模块分类：\n" + \
           "\n".join(f"{i+1}. {name}" for i, name in enumerate(ingredients))


# =========================
# 数据库操作
# =========================

def get_connection() -> pymysql.Connection:
    return pymysql.connect(**DB_CONFIG)


def collect_all_ingredients() -> List[str]:
    """从 catfood_standard_formula 收集所有去重原料。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT normalized_ingredients_json
                FROM {LABEL_DB}.catfood_standard_formula
                WHERE normalized_ingredients_json IS NOT NULL
                  AND normalized_ingredients_json != '[]'
                  AND normalized_ingredients_json != 'null'
            """)
            all_ingredients = set()
            for row in cur.fetchall():
                try:
                    items = json.loads(row["normalized_ingredients_json"])
                    if isinstance(items, list):
                        for item in items:
                            name = str(item).strip()
                            if name and len(name) >= 2 and not name.startswith("*"):
                                all_ingredients.add(name)
                except (json.JSONDecodeError, TypeError):
                    pass
        return sorted(all_ingredients)
    finally:
        conn.close()


def get_existing_ingredients() -> set:
    """获取分类表中已有的原料名。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT ingredient_name FROM {OUTPUT_TABLE}")
            return {row["ingredient_name"] for row in cur.fetchall()}
    finally:
        conn.close()


def seed_known_ingredients() -> int:
    """将种子规则写入分类表。"""
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    try:
        with conn.cursor() as cur:
            for rule in SEED_RULES:
                for keyword in rule["keywords"]:
                    cols = ["ingredient_name", "category", "classified_by", "classified_at"]
                    vals = [keyword, rule["category"], "keyword_seed", now]
                    for mod_col in MODULE_COLUMNS:
                        cols.append(mod_col)
                        vals.append(rule["modules"].get(mod_col, 0))
                    cols.append("base_score_override")
                    vals.append(rule.get("base_score"))

                    placeholders = ", ".join(["%s"] * len(vals))
                    col_names = ", ".join(f"`{c}`" for c in cols)
                    update_parts = ", ".join(
                        f"`{c}` = VALUES(`{c}`)" for c in cols if c != "ingredient_name"
                    )

                    sql = f"""
                        INSERT INTO {OUTPUT_TABLE} ({col_names})
                        VALUES ({placeholders})
                        ON DUPLICATE KEY UPDATE {update_parts}
                    """
                    cur.execute(sql, vals)
                    inserted += 1

        conn.commit()
        return inserted
    finally:
        conn.close()


def write_classifications(results: List[Dict[str, Any]]) -> int:
    """将 LLM 分类结果写入表。"""
    conn = get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    try:
        with conn.cursor() as cur:
            for item in results:
                name = item.get("name", "").strip()
                if not name:
                    continue
                category = item.get("category", "其他")
                modules = item.get("modules", [])
                if isinstance(modules, str):
                    modules = [modules]

                cols = ["ingredient_name", "category", "classified_by", "classified_at"]
                vals = [name, category, "llm", now]
                for mod_col in MODULE_COLUMNS:
                    cols.append(mod_col)
                    vals.append(1 if mod_col in modules else 0)

                placeholders = ", ".join(["%s"] * len(vals))
                col_names = ", ".join(f"`{c}`" for c in cols)
                update_parts = ", ".join(
                    f"`{c}` = VALUES(`{c}`)" for c in cols if c != "ingredient_name"
                )

                sql = f"""
                    INSERT INTO {OUTPUT_TABLE} ({col_names})
                    VALUES ({placeholders})
                    ON DUPLICATE KEY UPDATE {update_parts}
                """
                cur.execute(sql, vals)
                written += 1

        conn.commit()
        return written
    finally:
        conn.close()


def export_review_csv(path: str):
    """导出分类表为审核 CSV。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {OUTPUT_TABLE} ORDER BY category, ingredient_name")
            rows = cur.fetchall()

        if not rows:
            print("分类表为空，无需导出")
            return

        fieldnames = list(rows[0].keys())
        # Convert datetime objects
        for row in rows:
            for k, v in row.items():
                if isinstance(v, datetime):
                    row[k] = v.strftime("%Y-%m-%d %H:%M:%S")
                elif isinstance(v, bytes):
                    row[k] = v.decode("utf-8")

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        print(f"审核 CSV 已导出: {path} ({len(rows)} 行)")
    finally:
        conn.close()


# =========================
# LLM 分类
# =========================

def create_llm_client() -> OpenAI:
    api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("QWEN_API_KEY")
    if not api_key:
        raise RuntimeError("请设置 DASHSCOPE_API_KEY 或 QWEN_API_KEY")
    return OpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout=LLM_TIMEOUT,
    )


def classify_batch(client: OpenAI, ingredients: List[str], model: str) -> List[Dict[str, Any]]:
    """调用 LLM 对一批原料进行分类。"""
    prompt = build_batch_prompt(ingredients)

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            content = response.choices[0].message.content.strip()

            # Remove markdown code fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0]
            content = content.strip()

            result = json.loads(content)
            if isinstance(result, dict):
                result = [result]
            if isinstance(result, list):
                return result
            return []

        except json.JSONDecodeError as e:
            if attempt < MAX_RETRIES - 1:
                print(f"  JSON 解析失败，重试 {attempt + 1}/{MAX_RETRIES}: {e}")
                time.sleep(2)
            else:
                print(f"  JSON 解析最终失败: {content[:200]}")
                return []
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                print(f"  LLM 调用失败，重试 {attempt + 1}/{MAX_RETRIES}: {e}")
                time.sleep(3)
            else:
                print(f"  LLM 调用最终失败: {e}")
                return []

    return []


# =========================
# 主流程
# =========================

def main():
    parser = argparse.ArgumentParser(description="猫粮原料工艺模块分类")
    parser.add_argument("--export-only", action="store_true", help="只导出审核 CSV")
    parser.add_argument("--incremental", action="store_true", help="只分类新增原料")
    parser.add_argument("--model", default=os.getenv("QWEN_MODEL", "qwen-plus"),
                        help="LLM 模型名称")
    parser.add_argument("--dry-run", action="store_true", help="只打印待分类原料，不调 LLM")
    args = parser.parse_args()

    # Step 1: 收集所有原料
    print("收集原料 ...")
    all_ingredients = collect_all_ingredients()
    print(f"  总去重原料数: {len(all_ingredients)}")

    # Step 2: 种子写入
    if not args.export_only:
        print("写入种子规则 ...")
        seeded = seed_known_ingredients()
        print(f"  种子写入: {seeded} 条")

    # Step 3: 确定待分类原料
    existing = get_existing_ingredients()
    if args.incremental:
        to_classify = [i for i in all_ingredients if i not in existing]
    else:
        to_classify = [i for i in all_ingredients if i not in existing]

    print(f"  已分类: {len(existing)}")
    print(f"  待分类: {len(to_classify)}")

    if args.export_only:
        export_review_csv(REVIEW_CSV)
        return

    if args.dry_run:
        print("\n待分类原料列表:")
        for i, name in enumerate(to_classify[:50], 1):
            print(f"  {i}. {name}")
        if len(to_classify) > 50:
            print(f"  ... 还有 {len(to_classify) - 50} 个")
        return

    if not to_classify:
        print("所有原料已分类，无需调用 LLM")
        export_review_csv(REVIEW_CSV)
        return

    # Step 4: LLM 批量分类
    print(f"\n开始 LLM 分类 (模型: {args.model}, 批次大小: {BATCH_SIZE}) ...")
    client = create_llm_client()

    total_classified = 0
    total_failed = 0
    batch_count = 0

    for i in range(0, len(to_classify), BATCH_SIZE):
        batch = to_classify[i:i + BATCH_SIZE]
        batch_count += 1
        print(f"  批次 {batch_count}: {len(batch)} 个原料 ({i+1}-{i+len(batch)}/{len(to_classify)})")

        results = classify_batch(client, batch, args.model)

        if results:
            written = write_classifications(results)
            total_classified += written
            print(f"    成功写入: {written}")
        else:
            total_failed += len(batch)
            print(f"    失败: {len(batch)} 个")

        time.sleep(RATE_LIMIT_INTERVAL)

    # Step 5: 处理完全无法匹配的原料（标记为 "其他"）
    remaining = set(to_classify) - get_existing_ingredients()
    if remaining:
        print(f"\n写入 {len(remaining)} 个未匹配原料 (标记为 '其他') ...")
        fallback_results = [
            {"name": name, "category": "其他", "modules": []}
            for name in sorted(remaining)
        ]
        write_classifications(fallback_results)

    print(f"\n分类完成！")
    print(f"  LLM 分类: {total_classified}")
    print(f"  未匹配兜底: {len(remaining)}")
    print(f"  失败: {total_failed}")

    # Step 6: 导出审核 CSV
    export_review_csv(REVIEW_CSV)

    # Step 7: 统计
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {OUTPUT_TABLE}")
            total = cur.fetchone()["cnt"]
            cur.execute(f"SELECT category, COUNT(*) AS cnt FROM {OUTPUT_TABLE} GROUP BY category ORDER BY cnt DESC")
            cats = cur.fetchall()
        print(f"\n分类表总行数: {total}")
        print("分类分布:")
        for cat in cats:
            print(f"  {cat['category']}: {cat['cnt']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
