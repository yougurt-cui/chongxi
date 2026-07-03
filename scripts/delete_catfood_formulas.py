#!/usr/bin/env python3
"""Archive and delete invalid standard formulas and all current derived rows."""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config, get_mysql_config


AUDIT_TABLE = "catfood_formula_delete_audit"
MAPPING_TABLE = "catfood_ocr_standard_mapping"
CANDIDATE_TABLE = "catfood_standard_ingredient_candidate"
FORMULA_INPUT_TABLE = "catfood_formula_feature_input"
FORMULA_MASTER_TABLE = "catfood_standard_formula"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(type(value).__name__)


def _connect(config):
    return pymysql.connect(
        **config,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _formula_tables(cursor) -> list[str]:
    cursor.execute(
        """
        SELECT c.TABLE_NAME
        FROM INFORMATION_SCHEMA.COLUMNS c
        JOIN INFORMATION_SCHEMA.TABLES t
          ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME
        WHERE c.TABLE_SCHEMA=DATABASE()
          AND c.COLUMN_NAME='formula_id'
          AND t.TABLE_TYPE='BASE TABLE'
        GROUP BY c.TABLE_NAME
        ORDER BY c.TABLE_NAME
        """
    )
    return [
        row["TABLE_NAME"]
        for row in cursor.fetchall()
        if row["TABLE_NAME"] != AUDIT_TABLE
        and "_bak_" not in row["TABLE_NAME"]
        and not row["TABLE_NAME"].startswith("delbak_")
        and not row["TABLE_NAME"].startswith("cleanbak_")
        and not row["TABLE_NAME"].endswith("_backup")
    ]


def _select_rows(cursor, table: str, formula_ids: tuple[int, ...]) -> list[dict[str, Any]]:
    if not SAFE_NAME.fullmatch(table):
        raise ValueError(f"unsafe table name: {table!r}")
    placeholders = ",".join(["%s"] * len(formula_ids))
    cursor.execute(
        f"SELECT * FROM `{table}` WHERE formula_id IN ({placeholders})",
        formula_ids,
    )
    return list(cursor.fetchall())


def _ensure_audit_table(cursor) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{AUDIT_TABLE}` (
          audit_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          batch_id VARCHAR(32) NOT NULL,
          source_database VARCHAR(128) NOT NULL,
          source_table VARCHAR(128) NOT NULL,
          formula_id BIGINT NULL,
          row_data JSON NOT NULL,
          archived_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (audit_id),
          KEY idx_formula_delete_batch (batch_id),
          KEY idx_formula_delete_formula (formula_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def delete_formulas(formula_ids: list[int], *, apply: bool) -> dict[str, Any]:
    ids = tuple(sorted(set(int(value) for value in formula_ids)))
    if not ids:
        raise ValueError("formula_ids cannot be empty")
    batch_id = f"fd_{uuid.uuid4().hex[:12]}"
    csv_cfg = get_mysql_config()
    feature_cfg = get_feature_mysql_config()

    archived: list[tuple[str, str, int | None, str]] = []
    rows_by_db: dict[str, dict[str, list[dict[str, Any]]]] = {}
    contexts: dict[str, int] = {}
    for label, cfg in (("csv", csv_cfg), ("feature", feature_cfg)):
        with _connect(cfg) as conn:
            with conn.cursor() as cursor:
                tables = _formula_tables(cursor)
                table_rows = {
                    table: _select_rows(cursor, table, ids)
                    for table in tables
                }
                rows_by_db[label] = table_rows
                if label == "csv":
                    for row in table_rows.get(FORMULA_INPUT_TABLE, []):
                        context = str(row.get("ingredient_composition") or "")[:2000]
                        if context:
                            contexts[context] = int(row["formula_id"])
                    candidate_rows = []
                    for context in contexts:
                        cursor.execute(
                            f"SELECT * FROM `{CANDIDATE_TABLE}` WHERE context=%s",
                            (context,),
                        )
                        candidate_rows.extend(cursor.fetchall())
                    table_rows[CANDIDATE_TABLE] = candidate_rows

                for table, rows in table_rows.items():
                    for row in rows:
                        formula_id = row.get("formula_id")
                        if table == CANDIDATE_TABLE:
                            formula_id = contexts.get(str(row.get("context") or ""))
                        archived.append(
                            (
                                cfg["database"],
                                table,
                                int(formula_id) if formula_id is not None else None,
                                json.dumps(row, ensure_ascii=False, default=_json_default),
                            )
                        )
            conn.rollback()

    summary = {
        "ok": True,
        "applied": apply,
        "batch_id": batch_id if apply else None,
        "formula_ids": list(ids),
        "affected_rows": {
            label: {table: len(rows) for table, rows in tables.items() if rows}
            for label, tables in rows_by_db.items()
        },
        "audit_row_count": len(archived),
    }
    if not apply:
        return summary

    # Archive every affected row before changing either database.
    with _connect(csv_cfg) as audit_conn:
        with audit_conn.cursor() as cursor:
            _ensure_audit_table(cursor)
            cursor.executemany(
                f"""
                INSERT INTO `{AUDIT_TABLE}`(
                  batch_id, source_database, source_table, formula_id, row_data
                ) VALUES(%s, %s, %s, %s, %s)
                """,
                [(batch_id, *row) for row in archived],
            )
        audit_conn.commit()

    placeholders = ",".join(["%s"] * len(ids))
    # Feature DB contains derived rows only.
    with _connect(feature_cfg) as feature_conn:
        with feature_conn.cursor() as cursor:
            for table, rows in rows_by_db["feature"].items():
                if not rows:
                    continue
                cursor.execute(
                    f"DELETE FROM `{table}` WHERE formula_id IN ({placeholders})",
                    ids,
                )
        feature_conn.commit()

    with _connect(csv_cfg) as csv_conn:
        with csv_conn.cursor() as cursor:
            # Retain OCR lineage, but force formula standardization to run again.
            cursor.execute(
                f"""
                UPDATE `{MAPPING_TABLE}`
                SET formula_id=NULL,
                    formula_status='pending',
                    formula_confidence=NULL,
                    overall_status='pending',
                    match_evidence_json=JSON_SET(
                      COALESCE(match_evidence_json, JSON_OBJECT()),
                      '$.formula_delete_batch', %s
                    )
                WHERE formula_id IN ({placeholders})
                """,
                (batch_id, *ids),
            )
            for context in contexts:
                cursor.execute(
                    f"DELETE FROM `{CANDIDATE_TABLE}` WHERE context=%s",
                    (context,),
                )
            csv_tables = rows_by_db["csv"]
            delete_order = [
                table for table in csv_tables
                if table not in {MAPPING_TABLE, CANDIDATE_TABLE, FORMULA_MASTER_TABLE}
            ]
            for table in delete_order:
                if csv_tables[table]:
                    cursor.execute(
                        f"DELETE FROM `{table}` WHERE formula_id IN ({placeholders})",
                        ids,
                    )
            cursor.execute(
                f"DELETE FROM `{FORMULA_MASTER_TABLE}` WHERE formula_id IN ({placeholders})",
                ids,
            )
        csv_conn.commit()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("formula_ids", nargs="+", type=int)
    parser.add_argument("--apply", action="store_true", help="archive and delete; default previews")
    args = parser.parse_args()
    print(
        json.dumps(
            delete_formulas(args.formula_ids, apply=bool(args.apply)),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
