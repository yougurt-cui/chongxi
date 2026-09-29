"""Adapter around the existing Need/Decision/Experience/Switch rules."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pymysql

from app_config import get_mysql_config
from scripts.filter_catfood_choice_comments import (
    condition_metadata,
    ensure_output_table,
    find_named_signals,
    insert_batch,
    is_choice_comment,
)


_ENSURED_TABLES: set[str] = set()


def extract_choice(comment: Any, run_id: str) -> dict[str, Any]:
    context = " ".join(filter(None, [comment.source_title, comment.source_content, comment.source_keyword]))
    keep, signals, intents, score, mentions_brand, mentions_condition = is_choice_comment(
        comment.clean_text, context
    )
    if not keep:
        signals, intents, score = find_named_signals(comment.clean_text)
        mentions_brand = bool(comment.brand)
        mentions_condition = bool(condition_metadata(comment.clean_text, context)["mentions_condition"])
    condition = condition_metadata(comment.clean_text, context)
    return {
        "run_id": run_id,
        "source_platform": comment.platform,
        "source_schema": get_mysql_config()["database"],
        "source_table": comment.source_table,
        "source_row_id": comment.source_row_id,
        "external_id": comment.source_id,
        "source_record_key": comment.source_id,
        "comment_text": comment.raw_text,
        "normalized_text": comment.clean_text,
        "intent_labels": "、".join(dict.fromkeys(intents or ["Decision"])),
        "matched_signals": "、".join(dict.fromkeys(signals)),
        "choice_score": score,
        "mentions_brand": int(mentions_brand),
        "mentions_condition": int(mentions_condition),
        "condition_confidence": condition["confidence"],
        "condition_categories": condition["categories"],
        "condition_symptoms": condition["symptoms"],
        "condition_keywords": condition["keywords"],
        "source_title": comment.source_title,
        "source_content": comment.source_content,
        "source_like_count": comment.source_like_count,
        "source_comment_time": comment.source_created_at or None,
        "source_keyword": comment.source_keyword or None,
        "inserted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_choice(conn, output_table: str, row: dict[str, Any]) -> None:
    ensure_choice_table(conn, output_table)
    with conn.cursor() as cursor:
        cursor.execute(
            f"""SELECT id FROM `{output_table}`
            WHERE source_platform=%s AND source_table=%s AND source_record_key=%s LIMIT 1""",
            (row["source_platform"], row["source_table"], row["source_record_key"]),
        )
        if cursor.fetchone():
            return
    insert_batch(conn, output_table, [row])


def ensure_choice_table(conn, output_table: str) -> None:
    if output_table in _ENSURED_TABLES:
        return
    try:
        ensure_output_table(conn, output_table)
    except pymysql.err.IntegrityError as exc:
        # Legacy tables may already contain duplicate source keys. Do not delete
        # historical data implicitly; save_choice performs an explicit lookup.
        if not exc.args or exc.args[0] != 1062:
            raise
    _ENSURED_TABLES.add(output_table)
