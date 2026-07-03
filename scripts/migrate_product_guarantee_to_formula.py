#!/usr/bin/env python3
"""Migrate product_guarantee to formula grain without coupling it to Profile."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402
from services.catfood_standardization_service import normalize_ingredients  # noqa: E402

TABLE = "product_guarantee"
PROFILE_TABLE = "catfood_formula_feature_profile"
CORE_METRICS = ("粗蛋白", "粗脂肪", "粗纤维", "水分")
PROFILE_VALUE_COLUMNS = {
    "粗蛋白": "crude_protein_value",
    "粗脂肪": "crude_fat_value",
    "粗纤维": "crude_fiber_value",
    "水分": "moisture_value",
    "粗灰分": "ash_value",
    "钙": "calcium_value",
    "总磷": "phosphorus_value",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def _ddl(table: str) -> str:
    return f"""
        CREATE TABLE `{table}` (
          id BIGINT NOT NULL AUTO_INCREMENT,
          formula_id BIGINT UNSIGNED NOT NULL,
          source_id BIGINT NULL,
          parsed_row_id BIGINT NULL,
          image_name VARCHAR(255) NULL,
          file_sha256 CHAR(64) NULL,
          metric_name VARCHAR(100) NOT NULL,
          operator_symbol VARCHAR(10) NOT NULL DEFAULT '',
          metric_value DECIMAL(18,2) NOT NULL,
          metric_unit VARCHAR(50) NOT NULL,
          basis VARCHAR(50) NOT NULL DEFAULT '',
          raw_text VARCHAR(255) NULL,
          extract_batch_id VARCHAR(32) NOT NULL DEFAULT '',
          created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (id),
          UNIQUE KEY uq_source_metric (source_id, metric_name, basis, operator_symbol, metric_unit),
          KEY idx_formula_id (formula_id),
          KEY idx_source_id (source_id),
          KEY idx_parsed_row_id (parsed_row_id),
          KEY idx_file_sha256 (file_sha256)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """


def _ensure_profile_schema(cursor) -> None:
    cursor.execute(f"SHOW COLUMNS FROM `{PROFILE_TABLE}`")
    columns = {row["Field"] for row in cursor.fetchall()}
    additions = {
        "nutrition_profile_json": "JSON NULL",
        "nutrition_status": "VARCHAR(32) NULL",
        "crude_protein_value": "DECIMAL(18,2) NULL",
        "crude_fat_value": "DECIMAL(18,2) NULL",
        "crude_fiber_value": "DECIMAL(18,2) NULL",
        "moisture_value": "DECIMAL(18,2) NULL",
        "ash_value": "DECIMAL(18,2) NULL",
        "calcium_value": "DECIMAL(18,2) NULL",
        "phosphorus_value": "DECIMAL(18,2) NULL",
        "nutrition_updated_at": "DATETIME NULL",
    }
    for name, ddl in additions.items():
        if name not in columns:
            cursor.execute(f"ALTER TABLE `{PROFILE_TABLE}` ADD COLUMN `{name}` {ddl}")


def _load_formula_map(cursor) -> dict[str, int]:
    cursor.execute(
        """
        SELECT formula_id, normalized_ingredient_composition, ingredient_fingerprint
        FROM catfood_standard_formula
        WHERE status='active' AND is_current=1
        """
    )
    result = {}
    ambiguous = set()
    for row in cursor.fetchall():
        _, ingredients, calculated = normalize_ingredients(row["normalized_ingredient_composition"])
        fingerprint = str(row.get("ingredient_fingerprint") or calculated)
        if not ingredients:
            continue
        if fingerprint in result and result[fingerprint] != int(row["formula_id"]):
            ambiguous.add(fingerprint)
        else:
            result[fingerprint] = int(row["formula_id"])
    for fingerprint in ambiguous:
        result.pop(fingerprint, None)
    return result


def _load_parsed_formula_map(cursor, formula_map: dict[str, int]) -> tuple[dict[int, int], dict[int, int]]:
    cursor.execute("SELECT id, source_id, ingredient_composition FROM catfood_ingredient_ocr_parsed")
    by_parsed = {}
    by_source = {}
    for row in cursor.fetchall():
        _, ingredients, fingerprint = normalize_ingredients(row.get("ingredient_composition"))
        formula_id = formula_map.get(fingerprint) if ingredients else None
        if not formula_id:
            continue
        by_parsed[int(row["id"])] = formula_id
        if row.get("source_id") is not None:
            by_source[int(row["source_id"])] = formula_id
    return by_parsed, by_source


def _selected_measurements(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected = {}
    for row in sorted(
        rows,
        key=lambda item: (
            str(item.get("basis") or "") == "干物质",
            item.get("updated_at") or item.get("created_at") or datetime.min,
            int(item.get("id") or 0),
        ),
    ):
        selected[str(row["metric_name"])] = {
            "value": float(row["metric_value"]),
            "operator": str(row.get("operator_symbol") or ""),
            "unit": str(row.get("metric_unit") or ""),
            "basis": str(row.get("basis") or ""),
            "raw_text": row.get("raw_text"),
        }
    return selected


def migrate(*, apply: bool) -> dict[str, Any]:
    cfg = get_mysql_config()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{TABLE}_bak_formula_{timestamp}"
    stage = f"{TABLE}_stage_formula"
    with pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            formula_map = _load_formula_map(cursor)
            by_parsed, by_source = _load_parsed_formula_map(cursor, formula_map)
            cursor.execute(f"SELECT * FROM `{TABLE}` ORDER BY id")
            source_rows = list(cursor.fetchall())
            mapped = []
            unmapped = []
            for row in source_rows:
                formula_id = None
                if row.get("parsed_row_id") is not None:
                    formula_id = by_parsed.get(int(row["parsed_row_id"]))
                if not formula_id and row.get("source_id") is not None:
                    formula_id = by_source.get(int(row["source_id"]))
                if formula_id:
                    mapped.append({**row, "formula_id": formula_id})
                else:
                    unmapped.append(row)

            deduped = {}
            for row in mapped:
                key = (
                    int(row["formula_id"]), str(row["metric_name"]), str(row.get("basis") or ""),
                    str(row.get("operator_symbol") or ""), str(row["metric_unit"]),
                )
                current = deduped.get(key)
                rank = (row.get("updated_at") or row.get("created_at") or datetime.min, int(row["id"]))
                current_rank = (
                    current.get("updated_at") or current.get("created_at") or datetime.min,
                    int(current["id"]),
                ) if current else None
                if current is None or rank > current_rank:
                    deduped[key] = row

            by_formula: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for row in deduped.values():
                by_formula[int(row["formula_id"])].append(row)

            summary = {
                "ok": True,
                "applied": apply,
                "source_rows": len(source_rows),
                "mapped_source_rows": len(mapped),
                "unmapped_source_rows": len(unmapped),
                "deduped_formula_rows": len(deduped),
                "formula_count": len(by_formula),
                "backup_table": backup if apply else None,
            }
            if not apply:
                conn.rollback()
                return summary

            cursor.execute(f"CREATE TABLE `{backup}` LIKE `{TABLE}`")
            cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{TABLE}`")
            cursor.execute(f"DROP TABLE IF EXISTS `{stage}`")
            cursor.execute(_ddl(stage))
            insert_sql = f"""
                INSERT INTO `{stage}`(
                  formula_id, source_id, parsed_row_id, image_name, file_sha256,
                  metric_name, operator_symbol, metric_value, metric_unit, basis,
                  raw_text, extract_batch_id, created_at, updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """
            payload = []
            for row in deduped.values():
                payload.append((
                    row["formula_id"], row.get("source_id"), row.get("parsed_row_id"),
                    row.get("image_name"), row.get("file_sha256"), row["metric_name"],
                    row.get("operator_symbol") or "", row["metric_value"], row["metric_unit"],
                    row.get("basis") or "", row.get("raw_text"), row.get("extract_batch_id") or "",
                    row.get("created_at"), row.get("updated_at"),
                ))
            if payload:
                cursor.executemany(insert_sql, payload)
            cursor.execute(f"RENAME TABLE `{TABLE}` TO `{TABLE}_legacy_swap`, `{stage}` TO `{TABLE}`")
            cursor.execute(f"DROP TABLE `{TABLE}_legacy_swap`")

        conn.commit()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(apply=bool(args.apply)), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
