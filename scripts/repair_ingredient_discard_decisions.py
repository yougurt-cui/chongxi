#!/usr/bin/env python3
"""Repair known unsafe discard decisions made before deterministic guards existed."""
import json
import sys
from pathlib import Path
import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from app_config import get_mysql_config  # noqa: E402


def main() -> int:
    conn = pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    with conn, conn.cursor() as cursor:
        cursor.execute(
            """UPDATE catfood_standard_ingredient_candidate
               SET status='new_standard_reviewed', reviewer=NULL, reviewed_at=NULL,
                   review_note=CONCAT_WS('；',review_note,'系统纠正：含量说明不构成复合原料')
               WHERE status='discarded_compound' AND raw_name='马齿苋(100mg/kg)'"""
        )
        repaired = cursor.rowcount
        cursor.execute(
            """UPDATE catfood_standard_ingredient_candidate
               SET status='new_standard_reviewed', reviewer=NULL, reviewed_at=NULL,
                   review_note=CONCAT_WS('；',review_note,'系统纠正：归一键鱼油包含多个原文，禁止传播舍弃状态')
               WHERE normalized_raw_name='鱼油' AND status LIKE 'discarded_%'"""
        )
        repaired += cursor.rowcount
        cursor.execute(
            """UPDATE catfood_standard_ingredient_candidate
               SET status='discarded_noise', reviewer='system_noise_rule', reviewed_at=NOW(),
                   review_note=CONCAT_WS('；',review_note,'系统纠正：孤立编号/残片不是原材料')
               WHERE raw_name IN ('β-1','2') AND status='new_standard_reviewed'"""
        )
        repaired += cursor.rowcount
        conn.commit()
    print(json.dumps({"repaired": repaired}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
