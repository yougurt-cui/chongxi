#!/usr/bin/env python3
"""Align ingredient items and candidate scope to current standard formulas."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "vendor" / "feature_score_pipeline" / "scripts"
for path in (BASE_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app_config import get_mysql_config  # noqa: E402
from vendor.feature_score_pipeline.scripts.rebuild_protein_source_aggregate import (  # noqa: E402
    _normalize_ingredient_key,
)


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


def run(*, apply: bool) -> dict:
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT i.raw_name
            FROM catfood_formula_ingredient_item i
            JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
            WHERE i.standard_ingredient_id IS NULL AND COALESCE(i.is_ignored,0)=0
            """
        )
        current_keys = {
            _normalize_ingredient_key(row["raw_name"])
            for row in cursor.fetchall()
            if _normalize_ingredient_key(row["raw_name"])
        }
        cursor.execute(
            """
            SELECT COUNT(*) n, COUNT(DISTINCT i.formula_id) formulas_n
            FROM catfood_formula_ingredient_item i
            LEFT JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
            WHERE f.formula_id IS NULL
            """
        )
        orphan = dict(cursor.fetchone())
        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        backups = []
        archived = 0
        if apply:
            for table in (
                "catfood_formula_ingredient_item",
                "catfood_standard_ingredient_candidate",
            ):
                backup = f"{table}_scope_{suffix}"
                cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
                cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
                backups.append(backup)
            cursor.execute(
                """
                DELETE i FROM catfood_formula_ingredient_item i
                LEFT JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
                WHERE f.formula_id IS NULL
                """
            )
            deleted = int(cursor.rowcount)
            cursor.execute(
                "SELECT candidate_id, normalized_raw_name FROM catfood_standard_ingredient_candidate"
            )
            out_ids = [
                int(row["candidate_id"])
                for row in cursor.fetchall()
                if str(row["normalized_raw_name"] or "") not in current_keys
            ]
            for start in range(0, len(out_ids), 500):
                batch = out_ids[start : start + 500]
                placeholders = ",".join(["%s"] * len(batch))
                cursor.execute(
                    f"UPDATE catfood_standard_ingredient_candidate "
                    f"SET status='out_of_scope', review_note=CONCAT_WS('；',review_note,'当前标准配方未引用') "
                    f"WHERE candidate_id IN ({placeholders})",
                    batch,
                )
                archived += int(cursor.rowcount)
            conn.commit()
        else:
            deleted = int(orphan.get("n") or 0)
            archived = 0
            conn.rollback()
        return {
            "applied": apply,
            "orphan_formula_count": int(orphan.get("formulas_n") or 0),
            "deleted_item_rows": deleted,
            "current_unmatched_normalized_names": len(current_keys),
            "archived_candidate_rows": archived,
            "backups": backups,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
