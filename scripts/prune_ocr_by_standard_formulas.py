#!/usr/bin/env python3
"""Back up and prune OCR rows not used by current standard formulas."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


RESULT_TABLE = "catfood_ingredient_ocr_results"
PARSED_TABLE = "catfood_ingredient_ocr_parsed"
MAPPING_TABLE = "catfood_ocr_standard_mapping"


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


def _source_ids(value: object) -> set[int]:
    return {int(token) for token in re.findall(r"\d+", str(value or ""))}


def _backup(cursor, table: str, suffix: str) -> tuple[str, int]:
    backup = f"cleanbak_{suffix}_{table}"
    cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
    cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
    return backup, int(cursor.rowcount)


def prune(*, apply: bool) -> dict[str, object]:
    with _connect() as conn, conn.cursor() as cursor:
        cursor.execute("""
            SELECT DISTINCT x.parsed_row_id
            FROM catfood_formula_feature_input x
            JOIN catfood_standard_formula f ON f.formula_id=x.formula_id
            WHERE x.parsed_row_id IS NOT NULL
        """)
        keep_parsed_ids = {int(row["parsed_row_id"]) for row in cursor.fetchall()}

        keep_source_ids: set[int] = set()
        if keep_parsed_ids:
            placeholders = ",".join(["%s"] * len(keep_parsed_ids))
            cursor.execute(
                f"SELECT id, source_id, merged_source_ids FROM `{PARSED_TABLE}` WHERE id IN ({placeholders})",
                tuple(sorted(keep_parsed_ids)),
            )
            parsed_rows = list(cursor.fetchall())
            found_parsed_ids = {int(row["id"]) for row in parsed_rows}
            missing_parsed_ids = sorted(keep_parsed_ids - found_parsed_ids)
            if missing_parsed_ids:
                raise ValueError(f"有效配方引用了不存在的解析行: {missing_parsed_ids}")
            for row in parsed_rows:
                keep_source_ids.add(int(row["source_id"]))
                keep_source_ids.update(_source_ids(row.get("merged_source_ids")))

        cursor.execute(f"SELECT id FROM `{PARSED_TABLE}`")
        all_parsed_ids = {int(row["id"]) for row in cursor.fetchall()}
        cursor.execute(f"SELECT id FROM `{RESULT_TABLE}`")
        all_source_ids = {int(row["id"]) for row in cursor.fetchall()}
        missing_source_ids = sorted(keep_source_ids - all_source_ids)
        if missing_source_ids:
            raise ValueError(f"有效解析行引用了不存在的OCR来源: {missing_source_ids}")

        delete_parsed_ids = sorted(all_parsed_ids - keep_parsed_ids)
        delete_source_ids = sorted(all_source_ids - keep_source_ids)
        cursor.execute("""
            SELECT m.mapping_id
            FROM catfood_ocr_standard_mapping m
            LEFT JOIN catfood_standard_formula f ON f.formula_id=m.formula_id
            WHERE f.formula_id IS NULL
        """)
        delete_mapping_ids = [int(row["mapping_id"]) for row in cursor.fetchall()]

        result: dict[str, object] = {
            "applied": apply,
            "keep": {
                "parsed_rows": len(keep_parsed_ids),
                "ocr_results": len(keep_source_ids),
            },
            "delete": {
                "parsed_rows": len(delete_parsed_ids),
                "ocr_results": len(delete_source_ids),
                "standard_mappings": len(delete_mapping_ids),
            },
            "delete_parsed_ids": delete_parsed_ids,
            "delete_source_ids": delete_source_ids,
            "delete_mapping_ids": delete_mapping_ids,
        }
        if not apply:
            conn.rollback()
            return result

        suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        result["backups"] = []
        for table in (RESULT_TABLE, PARSED_TABLE, MAPPING_TABLE):
            backup, row_count = _backup(cursor, table, suffix)
            result["backups"].append({"table": table, "backup": backup, "rows": row_count})

        for table, column, ids in (
            (MAPPING_TABLE, "mapping_id", delete_mapping_ids),
            (PARSED_TABLE, "id", delete_parsed_ids),
            (RESULT_TABLE, "id", delete_source_ids),
        ):
            if not ids:
                continue
            placeholders = ",".join(["%s"] * len(ids))
            cursor.execute(f"DELETE FROM `{table}` WHERE `{column}` IN ({placeholders})", ids)
            if cursor.rowcount != len(ids):
                raise RuntimeError(f"{table} expected delete {len(ids)}, got {cursor.rowcount}")
        conn.commit()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit changes; default previews")
    args = parser.parse_args()
    print(json.dumps(prune(apply=bool(args.apply)), ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
