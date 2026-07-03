#!/usr/bin/env python3
"""Merge active standard ingredients with the same normalized standard name."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

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


TABLES = (
    "catfood_standard_ingredient",
    "catfood_standard_ingredient_alias",
    "catfood_standard_ingredient_candidate",
    "catfood_formula_ingredient_item",
)


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


def _backup(cursor, table: str, suffix: str) -> str:
    backup = f"{table}_dup_{suffix}"
    cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
    cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
    return backup


def _choose_canonical(
    rows: list[dict[str, Any]], alias_counts: dict[str, int], item_counts: dict[str, int]
) -> dict[str, Any]:
    # Prefer IDs already used by formula items, then the richer alias record.
    return sorted(
        rows,
        key=lambda row: (
            -item_counts.get(row["standard_ingredient_id"], 0),
            -alias_counts.get(row["standard_ingredient_id"], 0),
            row["standard_ingredient_id"],
        ),
    )[0]


def merge_duplicates(*, apply: bool) -> dict[str, Any]:
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM catfood_standard_ingredient WHERE active=1 "
            "ORDER BY standard_ingredient_id"
        )
        standards = list(cursor.fetchall())
        cursor.execute(
            "SELECT standard_ingredient_id, COUNT(*) n "
            "FROM catfood_standard_ingredient_alias WHERE active=1 GROUP BY standard_ingredient_id"
        )
        alias_counts = {row["standard_ingredient_id"]: int(row["n"]) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT standard_ingredient_id, COUNT(*) n FROM catfood_formula_ingredient_item "
            "WHERE standard_ingredient_id IS NOT NULL GROUP BY standard_ingredient_id"
        )
        item_counts = {row["standard_ingredient_id"]: int(row["n"]) for row in cursor.fetchall()}

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in standards:
            grouped[_normalize_ingredient_key(row["standard_name"])].append(row)

        plans = []
        for normalized_name, rows in grouped.items():
            if len(rows) < 2:
                continue
            canonical = _choose_canonical(rows, alias_counts, item_counts)
            duplicate_ids = [
                row["standard_ingredient_id"]
                for row in rows
                if row["standard_ingredient_id"] != canonical["standard_ingredient_id"]
            ]
            plans.append(
                {
                    "normalized_name": normalized_name,
                    "canonical_id": canonical["standard_ingredient_id"],
                    "canonical_name": canonical["standard_name"],
                    "duplicate_ids": duplicate_ids,
                }
            )

        backups: list[str] = []
        if apply and plans:
            suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
            backups = [_backup(cursor, table, suffix) for table in TABLES]
            for plan in plans:
                canonical_id = plan["canonical_id"]
                for duplicate_id in plan["duplicate_ids"]:
                    cursor.execute(
                        "UPDATE catfood_standard_ingredient_alias "
                        "SET standard_ingredient_id=%s WHERE standard_ingredient_id=%s",
                        (canonical_id, duplicate_id),
                    )
                    cursor.execute(
                        "UPDATE catfood_formula_ingredient_item i "
                        "JOIN catfood_standard_ingredient s ON s.standard_ingredient_id=%s "
                        "SET i.standard_ingredient_id=%s, i.standard_name=s.standard_name, "
                        "i.ingredient_family=s.ingredient_family, i.source_type=s.source_type, "
                        "i.animal_source=s.animal_source, "
                        "i.primary_nutrition_role=s.primary_nutrition_role "
                        "WHERE i.standard_ingredient_id=%s",
                        (canonical_id, canonical_id, duplicate_id),
                    )
                    cursor.execute(
                        "UPDATE catfood_standard_ingredient_candidate "
                        "SET suggested_standard_ingredient_id=%s, suggested_standard_name=%s "
                        "WHERE suggested_standard_ingredient_id=%s",
                        (canonical_id, plan["canonical_name"], duplicate_id),
                    )
                    cursor.execute(
                        "UPDATE catfood_standard_ingredient SET active=0, updated_at=NOW() "
                        "WHERE standard_ingredient_id=%s",
                        (duplicate_id,),
                    )
            conn.commit()
        else:
            conn.rollback()

    return {"applied": apply, "duplicate_groups": len(plans), "plans": plans, "backups": backups}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = merge_duplicates(apply=args.apply)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.report:
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
