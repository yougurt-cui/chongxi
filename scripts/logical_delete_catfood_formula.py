#!/usr/bin/env python3
"""Logically delete a standard formula while preserving its full lineage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config


def logical_delete_formula(formula_id: int, *, reason: str, apply: bool) -> dict[str, Any]:
    conn = pymysql.connect(
        **get_mysql_config(database="csv_labeling"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT formula_id,product_id,status,is_current "
                "FROM catfood_standard_formula WHERE formula_id=%s FOR UPDATE",
                (formula_id,),
            )
            before = cursor.fetchone()
            if not before:
                raise KeyError(f"formula_id 不存在: {formula_id}")

            affected = {}
            statements = [
                (
                    "catfood_standard_formula",
                    "UPDATE catfood_standard_formula "
                    "SET status='deleted',is_current=0 WHERE formula_id=%s",
                    (formula_id,),
                ),
                (
                    "catfood_formula_feature_input",
                    "UPDATE catfood_formula_feature_input "
                    "SET build_status='deleted',is_current=0 WHERE formula_id=%s",
                    (formula_id,),
                ),
                (
                    "catfood_formula_feature_profile",
                    "UPDATE catfood_formula_feature_profile "
                    "SET overall_status='deleted',protein_status='deleted',fat_status='deleted',"
                    "fiber_status='deleted',starch_status='deleted' WHERE formula_id=%s",
                    (formula_id,),
                ),
                (
                    "catfood_ocr_standard_mapping",
                    "UPDATE catfood_ocr_standard_mapping "
                    "SET formula_status='deleted',overall_status='deleted',"
                    "review_note=CONCAT_WS('；',NULLIF(review_note,''),%s),"
                    "reviewer='Codex',reviewed_at=NOW() WHERE formula_id=%s",
                    (reason, formula_id),
                ),
            ]
            for table, sql, params in statements:
                cursor.execute(sql, params)
                affected[table] = int(cursor.rowcount)

            result = {
                "ok": True,
                "applied": apply,
                "formula_id": formula_id,
                "before": before,
                "affected_rows": affected,
                "reason": reason,
            }
        if apply:
            conn.commit()
        else:
            conn.rollback()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("formula_id", type=int)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(
        logical_delete_formula(args.formula_id, reason=args.reason, apply=args.apply),
        ensure_ascii=False,
        indent=2,
        default=str,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
