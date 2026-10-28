"""Lightweight provisional/official metadata synchronization for score results."""

from __future__ import annotations

import json
from typing import Any

import pymysql

from app_config import get_feature_mysql_config, get_mysql_config


def _formula_result_metadata(formula_id: int) -> dict[str, Any]:
    with pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT input_hash,result_stage,quality_flags_json
                FROM catfood_formula_feature_input
                WHERE formula_id=%s
                """,
                (int(formula_id),),
            )
            row = cursor.fetchone() or {}
    flags = row.get("quality_flags_json") or []
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except (TypeError, ValueError):
            flags = []
    return {
        "input_fingerprint": row.get("input_hash"),
        "result_stage": row.get("result_stage") or "provisional",
        "quality_flags": flags if isinstance(flags, list) else [],
    }


def sync_formula_result_metadata(
    formula_id: int,
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Attach input provenance to risk rows without importing score scripts."""
    metadata = _formula_result_metadata(int(formula_id))
    with pymysql.connect(
        **get_feature_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW COLUMNS FROM sku_risk_score_result")
            columns = {row["Field"] for row in cursor.fetchall()}
            if "result_stage" not in columns:
                cursor.execute(
                    "ALTER TABLE sku_risk_score_result "
                    "ADD COLUMN result_stage VARCHAR(32) NOT NULL DEFAULT 'official'"
                )
            if "input_fingerprint" not in columns:
                cursor.execute(
                    "ALTER TABLE sku_risk_score_result "
                    "ADD COLUMN input_fingerprint CHAR(64) NULL"
                )
            if "quality_flags_json" not in columns:
                cursor.execute(
                    "ALTER TABLE sku_risk_score_result "
                    "ADD COLUMN quality_flags_json JSON NULL"
                )
            where = "formula_id=%s"
            params: list[Any] = [
                metadata["result_stage"],
                metadata["input_fingerprint"],
                json.dumps(metadata["quality_flags"], ensure_ascii=False),
                int(formula_id),
            ]
            if batch_id:
                where += " AND batch_id=%s"
                params.append(batch_id)
            else:
                where += " AND input_fingerprint=%s"
                params.append(metadata['input_fingerprint'])
            cursor.execute(
                f"""
                UPDATE sku_risk_score_result
                SET result_stage=%s,input_fingerprint=%s,quality_flags_json=%s
                WHERE {where}
                """,
                params,
            )
            updated = cursor.rowcount
        conn.commit()
    return {**metadata, "updated_result_count": updated}
