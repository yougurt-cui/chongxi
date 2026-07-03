#!/usr/bin/env python3
"""Normalize and deduplicate standard ingredient candidate names."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "vendor" / "feature_score_pipeline" / "scripts"
for path in (BASE_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app_config import get_mysql_config  # noqa: E402
import rebuild_protein_source_aggregate as protein_aggregate  # noqa: E402


CANDIDATE_TABLE = "catfood_standard_ingredient_candidate"
STATUS_PRIORITY = {"approved": 3, "pending": 2, "rejected": 1}


def _connect():
    return pymysql.connect(
        **get_mysql_config(),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _survivor(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        rows,
        key=lambda row: (
            STATUS_PRIORITY.get(str(row.get("status") or ""), 0),
            bool(row.get("suggested_standard_ingredient_id")),
            bool(row.get("reviewed_at")),
            int(row["candidate_id"]),
        ),
    )


def normalize_candidates(*, apply: bool) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM `{CANDIDATE_TABLE}` ORDER BY candidate_id")
            rows = list(cursor.fetchall())

            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            filtered: list[dict[str, Any]] = []
            changed: list[dict[str, Any]] = []
            for row in rows:
                normalized = protein_aggregate._normalize_ingredient_key(row.get("raw_name"))
                noise_reason = protein_aggregate._ingredient_candidate_noise_reason(normalized)
                if noise_reason:
                    filtered.append(
                        {
                            "candidate_id": int(row["candidate_id"]),
                            "raw_name": row.get("raw_name"),
                            "normalized_raw_name": normalized,
                            "reason": noise_reason,
                        }
                    )
                    continue
                row["new_normalized_raw_name"] = normalized
                groups[normalized].append(row)
                if normalized != row.get("normalized_raw_name"):
                    changed.append(
                        {
                            "candidate_id": int(row["candidate_id"]),
                            "raw_name": row.get("raw_name"),
                            "before": row.get("normalized_raw_name"),
                            "after": normalized,
                        }
                    )

            duplicate_ids: list[int] = []
            survivors: list[dict[str, Any]] = []
            review_conflicts: list[dict[str, Any]] = []
            for normalized, group in groups.items():
                survivor = _survivor(group)
                survivors.append(survivor)
                approved_targets = {
                    str(item.get("suggested_standard_ingredient_id"))
                    for item in group
                    if item.get("status") == "approved"
                    and item.get("suggested_standard_ingredient_id")
                }
                if len(approved_targets) > 1:
                    review_conflicts.append(
                        {
                            "normalized_raw_name": normalized,
                            "candidate_ids": [int(item["candidate_id"]) for item in group],
                            "approved_targets": sorted(approved_targets),
                        }
                    )
                    continue
                duplicate_ids.extend(
                    int(item["candidate_id"])
                    for item in group
                    if item["candidate_id"] != survivor["candidate_id"]
                )

            if apply:
                delete_ids = [item["candidate_id"] for item in filtered] + duplicate_ids
                if delete_ids:
                    placeholders = ",".join(["%s"] * len(delete_ids))
                    cursor.execute(
                        f"DELETE FROM `{CANDIDATE_TABLE}` WHERE candidate_id IN ({placeholders})",
                        tuple(delete_ids),
                    )
                conflict_ids = {
                    candidate_id
                    for conflict in review_conflicts
                    for candidate_id in conflict["candidate_ids"]
                }
                for survivor in survivors:
                    if int(survivor["candidate_id"]) in conflict_ids:
                        continue
                    cursor.execute(
                        f"""
                        UPDATE `{CANDIDATE_TABLE}`
                        SET normalized_raw_name=%s, updated_at=NOW()
                        WHERE candidate_id=%s
                        """,
                        (
                            survivor["new_normalized_raw_name"],
                            int(survivor["candidate_id"]),
                        ),
                    )
                conn.commit()
            else:
                conn.rollback()

    return {
        "ok": not review_conflicts,
        "applied": apply,
        "scanned": len(rows),
        "normalized_changed": len(changed),
        "noise_filtered": len(filtered),
        "noise_reason_counts": {
            reason: sum(item["reason"] == reason for item in filtered)
            for reason in sorted({item["reason"] for item in filtered})
        },
        "duplicates_merged": len(duplicate_ids),
        "review_conflicts": review_conflicts,
        "changed_samples": changed[:20],
        "filtered_samples": filtered[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit changes; default previews")
    args = parser.parse_args()
    print(json.dumps(normalize_candidates(apply=bool(args.apply)), ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
