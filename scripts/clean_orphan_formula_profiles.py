#!/usr/bin/env python3
"""Back up and remove formula profiles not present in the standard formula table."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )
    with conn, conn.cursor() as cursor:
        cursor.execute(
            """SELECT p.formula_id FROM catfood_formula_feature_profile p
               LEFT JOIN catfood_standard_formula f ON f.formula_id=p.formula_id
               WHERE f.formula_id IS NULL ORDER BY p.formula_id"""
        )
        orphan_ids = [int(row["formula_id"]) for row in cursor.fetchall()]
        backup = None
        deleted = 0
        if args.apply and orphan_ids:
            suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"catfood_formula_feature_profile_orphan_{suffix}"
            cursor.execute(f"CREATE TABLE `{backup}` LIKE catfood_formula_feature_profile")
            cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM catfood_formula_feature_profile")
            cursor.execute(
                """DELETE p FROM catfood_formula_feature_profile p
                   LEFT JOIN catfood_standard_formula f ON f.formula_id=p.formula_id
                   WHERE f.formula_id IS NULL"""
            )
            deleted = int(cursor.rowcount)
            conn.commit()
        else:
            conn.rollback()
        print(json.dumps({
            "applied": args.apply,
            "orphan_formula_ids": orphan_ids,
            "deleted": deleted,
            "backup": backup,
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
