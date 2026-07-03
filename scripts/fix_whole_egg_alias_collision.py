#!/usr/bin/env python3
"""Merge 全蛋 into the canonical 鸡蛋 standard ingredient STD00087."""
import json
import sys
from datetime import datetime
from pathlib import Path
import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from app_config import get_mysql_config  # noqa: E402


def main() -> int:
    conn = pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with conn, conn.cursor() as cursor:
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backups = []
        for table in (
            "catfood_standard_ingredient",
            "catfood_standard_ingredient_alias",
            "catfood_formula_ingredient_item",
            "catfood_standard_ingredient_candidate",
        ):
            backup = f"{table}_whole_egg_{suffix}"
            cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
            cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
            backups.append(backup)
        cursor.execute(
            """UPDATE catfood_standard_ingredient_alias
               SET standard_ingredient_id='STD00087', normalized_alias='鸡蛋', active=1
               WHERE standard_ingredient_id='STD00403' AND alias_name='全蛋'"""
        )
        migrated_aliases = cursor.rowcount
        cursor.execute(
            """INSERT INTO catfood_standard_ingredient_alias(
                 standard_ingredient_id,alias_name,normalized_alias,source,confidence,active)
               SELECT 'STD00087','全蛋','鸡蛋','manual_merge',1,1
               WHERE NOT EXISTS(SELECT 1 FROM catfood_standard_ingredient_alias
                 WHERE standard_ingredient_id='STD00087' AND alias_name='全蛋' AND active=1)"""
        )
        inserted_aliases = cursor.rowcount
        cursor.execute(
            """UPDATE catfood_standard_ingredient_alias
               SET active=0
               WHERE standard_ingredient_id='STD00087' AND alias_name='全蛋' AND active=1
                 AND alias_id<>(SELECT keep_id FROM (
                   SELECT MIN(alias_id) keep_id FROM catfood_standard_ingredient_alias
                   WHERE standard_ingredient_id='STD00087' AND alias_name='全蛋' AND active=1
                 ) x)"""
        )
        deactivated_duplicate_aliases = cursor.rowcount
        cursor.execute(
            """UPDATE catfood_formula_ingredient_item i
               JOIN catfood_standard_ingredient s ON s.standard_ingredient_id='STD00087'
               SET i.standard_ingredient_id='STD00087',i.standard_name=s.standard_name,
                   i.ingredient_family=s.ingredient_family,i.source_type=s.source_type,
                   i.animal_source=s.animal_source,
                   i.primary_nutrition_role=s.primary_nutrition_role
               WHERE i.standard_ingredient_id='STD00403'"""
        )
        migrated_items = cursor.rowcount
        cursor.execute(
            "UPDATE catfood_standard_ingredient SET active=0,updated_at=NOW() WHERE standard_ingredient_id='STD00403'"
        )
        deactivated_standards = cursor.rowcount
        cursor.execute(
            """UPDATE catfood_standard_ingredient_candidate
               SET status='approved', suggested_standard_ingredient_id='STD00087',
                   suggested_standard_name='鸡蛋', reviewer='system_alias_resolution',
                   review_note='全蛋已作为鸡蛋别名，唯一映射STD00087', reviewed_at=NOW()
               WHERE raw_name='全蛋'"""
        )
        candidates = cursor.rowcount
        conn.commit()
    print(json.dumps({"migrated_aliases": migrated_aliases, "inserted_aliases": inserted_aliases,
      "deactivated_duplicate_aliases": deactivated_duplicate_aliases,
      "migrated_items": migrated_items, "deactivated_standards": deactivated_standards,
      "updated_candidates": candidates, "backups": backups}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
