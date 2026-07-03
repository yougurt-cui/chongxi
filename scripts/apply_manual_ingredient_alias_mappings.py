#!/usr/bin/env python3
"""Apply reviewed ingredient aliases and merge redundant standards."""
import json
import sys
from datetime import datetime
from pathlib import Path
import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from app_config import get_mysql_config  # noqa: E402

ALIASES = {
    "火鸡肉粉": "STD00052",
    "干紫花苜蓿粉": "STD00705",
    "晒干苜蓿粉": "STD00705",
    "天然鸡肉香料": "STD00798",
    "干燥干酪乳杆菌发酵产物": "STD00118",
    "干凝结芽孢杆菌发酵产物": "STD00142",
    "白鱼肉粉(太平洋鱼太平洋鳕鱼、太平洋鳎目鱼、太平洋岩鱼)": "STD00072",
    "海洋鱼粉": "STD00072",
    "鲑鱼粉": "STD00072",
    "火鸡粉": "STD00052",
    "苜蓿": "STD00705",
    "苜蓿干草": "STD00705",
    "脱水苜蓿粉": "STD00705",
    "苜蓿芽": "STD00705",
    "海洋鱼（鳕鱼、鳀鱼、玉筋鱼、三文鱼）": "STD00861",
    "海洋鱼(鳕鱼、鳀鱼、玉筋鱼、三文鱼)": "STD00861",
    "鸡肉干": "STD00025",
    "脱骨鲜鸡肉": "STD00025",
}
MERGES = {"STD00561": "STD00072", "STD00774": "STD00072"}


def main() -> int:
    conn = pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with conn, conn.cursor() as cursor:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backups = []
        for table in ("catfood_standard_ingredient", "catfood_standard_ingredient_alias",
                      "catfood_formula_ingredient_item", "catfood_standard_ingredient_candidate"):
            backup = f"{table}_manualmap_{suffix}"
            cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
            cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
            backups.append(backup)
        merged_items = merged_aliases = 0
        cursor.execute(
            """INSERT INTO catfood_standard_ingredient(
              standard_ingredient_id,standard_name,ingredient_family,source_type,
              animal_source,primary_nutrition_role,active)
              VALUES('STD00861','鱼油','动物油脂类','animal','鱼','脂肪酸支持',1)
              ON DUPLICATE KEY UPDATE standard_name=VALUES(standard_name),active=1,updated_at=NOW()"""
        )
        created_fish_oil_standard = cursor.rowcount
        for old_id, target_id in MERGES.items():
            cursor.execute("UPDATE catfood_standard_ingredient_alias SET standard_ingredient_id=%s WHERE standard_ingredient_id=%s", (target_id, old_id))
            merged_aliases += cursor.rowcount
            cursor.execute(
                """UPDATE catfood_formula_ingredient_item i JOIN catfood_standard_ingredient s
                   ON s.standard_ingredient_id=%s SET i.standard_ingredient_id=%s,
                   i.standard_name=s.standard_name,i.ingredient_family=s.ingredient_family,
                   i.source_type=s.source_type,i.animal_source=s.animal_source,
                   i.primary_nutrition_role=s.primary_nutrition_role WHERE i.standard_ingredient_id=%s""",
                (target_id, target_id, old_id),
            )
            merged_items += cursor.rowcount
            cursor.execute("UPDATE catfood_standard_ingredient SET active=0,updated_at=NOW() WHERE standard_ingredient_id=%s", (old_id,))
        inserted = deactivated_conflicts = updated_candidates = 0
        for alias_name, target_id in ALIASES.items():
            cursor.execute(
                "UPDATE catfood_standard_ingredient_alias SET active=0 WHERE alias_name=%s AND standard_ingredient_id<>%s AND active=1",
                (alias_name, target_id),
            )
            deactivated_conflicts += cursor.rowcount
            cursor.execute("SELECT standard_name FROM catfood_standard_ingredient WHERE standard_ingredient_id=%s AND active=1", (target_id,))
            target = cursor.fetchone()
            if not target:
                raise ValueError(f"target standard is missing or inactive: {target_id}")
            cursor.execute(
                """INSERT INTO catfood_standard_ingredient_alias(
                  standard_ingredient_id,alias_name,normalized_alias,source,confidence,active)
                  SELECT %s,%s,%s,'manual_review',1,1 WHERE NOT EXISTS(
                    SELECT 1 FROM catfood_standard_ingredient_alias
                    WHERE standard_ingredient_id=%s AND alias_name=%s AND active=1)""",
                (target_id, alias_name, target["standard_name"], target_id, alias_name),
            )
            inserted += cursor.rowcount
            cursor.execute(
                """UPDATE catfood_standard_ingredient_candidate SET status='approved',
                   suggested_standard_ingredient_id=%s,suggested_standard_name=%s,
                   reviewer='manual_review',review_note='人工确认别名映射',reviewed_at=NOW()
                   WHERE raw_name=%s""",
                (target_id, target["standard_name"], alias_name),
            )
            updated_candidates += cursor.rowcount
        cursor.execute(
            """UPDATE catfood_standard_ingredient_candidate
               SET status='discarded_compound',reviewer='system_structural_rule',reviewed_at=NOW(),
                   review_note='矿物质群为多成分复合列表，不作为单一标准原材料'
               WHERE raw_name REGEXP '^矿物(质|群).*[、,，]'"""
        )
        discarded_mineral_candidates = cursor.rowcount
        # Keep one active row for each reviewed alias/target pair.
        for alias_name, target_id in ALIASES.items():
            cursor.execute(
                """UPDATE catfood_standard_ingredient_alias SET active=0
                   WHERE alias_name=%s AND standard_ingredient_id=%s AND active=1
                     AND alias_id<>(SELECT keep_id FROM (SELECT MIN(alias_id) keep_id
                       FROM catfood_standard_ingredient_alias WHERE alias_name=%s
                         AND standard_ingredient_id=%s AND active=1) x)""",
                (alias_name, target_id, alias_name, target_id),
            )
        conn.commit()
    print(json.dumps({"inserted_aliases": inserted, "deactivated_conflicts": deactivated_conflicts,
      "merged_aliases": merged_aliases, "merged_items": merged_items,
      "created_fish_oil_standard": created_fish_oil_standard,
      "updated_candidates": updated_candidates,
      "discarded_mineral_candidates": discarded_mineral_candidates,
      "backups": backups}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
