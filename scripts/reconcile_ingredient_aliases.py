#!/usr/bin/env python3
"""Reconcile ingredient candidates against active aliases and standard names."""

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
import rebuild_protein_source_aggregate as protein_aggregate  # noqa: E402


CANDIDATE_TABLE = "catfood_standard_ingredient_candidate"
ALIAS_TABLE = "catfood_standard_ingredient_alias"
STANDARD_TABLE = "catfood_standard_ingredient"


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


def _key(value: Any) -> str:
    return protein_aggregate._normalize_ingredient_key(value)


def _backup(cursor, table: str, suffix: str) -> str:
    backup = f"{table}_bak_{suffix}"
    cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
    cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
    return backup


def reconcile(*, apply: bool) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM `{STANDARD_TABLE}` WHERE active=1 ORDER BY standard_ingredient_id"
            )
            standards = list(cursor.fetchall())
            standard_by_id = {row["standard_ingredient_id"]: row for row in standards}

            cursor.execute(f"SELECT * FROM `{ALIAS_TABLE}` ORDER BY standard_ingredient_id")
            grouped_aliases = list(cursor.fetchall())
            aliases = []
            for grouped_alias in grouped_aliases:
                for alias_name in protein_aggregate._split_grouped_alias_names(
                    grouped_alias.get("alias_names")
                ):
                    aliases.append({
                        "standard_ingredient_id": grouped_alias["standard_ingredient_id"],
                        "alias_name": alias_name,
                    })
            cursor.execute(f"SELECT * FROM `{CANDIDATE_TABLE}` ORDER BY candidate_id")
            candidates = list(cursor.fetchall())

            lookup: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
            for standard in standards:
                lookup[_key(standard["standard_name"])][standard["standard_ingredient_id"]] = {
                    "standard_ingredient_id": standard["standard_ingredient_id"],
                    "standard_name": standard["standard_name"],
                    "matched_by": "standard_name",
                }
            orphan_aliases = []
            for alias in aliases:
                standard = standard_by_id.get(alias["standard_ingredient_id"])
                if not standard:
                    orphan_aliases.append(alias)
                    continue
                lookup[_key(alias["alias_name"])][standard["standard_ingredient_id"]] = {
                    "standard_ingredient_id": standard["standard_ingredient_id"],
                    "standard_name": standard["standard_name"],
                    "matched_by": "alias",
                }

            unique_matches = []
            ambiguous = []
            unmatched = []
            for candidate in candidates:
                key = _key(candidate["raw_name"])
                targets = list(lookup.get(key, {}).values())
                item = {
                    "candidate_id": int(candidate["candidate_id"]),
                    "raw_name": candidate["raw_name"],
                    "normalized_raw_name": key,
                    "previous_status": candidate.get("status"),
                }
                if len(targets) == 1:
                    item.update(targets[0])
                    unique_matches.append(item)
                elif len(targets) > 1:
                    item["targets"] = sorted(targets, key=lambda row: row["standard_ingredient_id"])
                    ambiguous.append(item)
                else:
                    unmatched.append(item)

            # Orphan aliases are repaired only where the alias name has exactly one active
            # standard-name target. Otherwise they are deactivated instead of guessed.
            orphan_repairs = []
            orphan_deactivations = []
            standard_name_lookup: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for standard in standards:
                standard_name_lookup[_key(standard["standard_name"])].append(standard)
            for alias in orphan_aliases:
                targets = standard_name_lookup.get(_key(alias["alias_name"]), [])
                if len(targets) == 1:
                    orphan_repairs.append(
                        {
                            "alias_name": alias["alias_name"],
                            "old_standard_ingredient_id": alias["standard_ingredient_id"],
                            "standard_ingredient_id": targets[0]["standard_ingredient_id"],
                            "standard_name": targets[0]["standard_name"],
                        }
                    )
                else:
                    orphan_deactivations.append(
                        {
                            "alias_name": alias["alias_name"],
                            "old_standard_ingredient_id": alias["standard_ingredient_id"],
                        }
                    )

            collision_groups = []
            alias_targets: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
                lambda: defaultdict(list)
            )
            for alias in aliases:
                if alias["standard_ingredient_id"] in standard_by_id:
                    alias_targets[_key(alias["alias_name"])][alias["standard_ingredient_id"]].append(alias)
            for key, targets in alias_targets.items():
                if len(targets) > 1:
                    collision_groups.append(
                        {
                            "normalized_alias": key,
                            "targets": [
                                {
                                    "standard_ingredient_id": standard_id,
                                    "standard_name": standard_by_id[standard_id]["standard_name"],
                                    "alias_count": len(rows),
                                }
                                for standard_id, rows in sorted(targets.items())
                            ],
                        }
                    )

            backups = []
            if apply:
                suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
                backups = [
                    _backup(cursor, ALIAS_TABLE, suffix),
                    _backup(cursor, CANDIDATE_TABLE, suffix),
                ]
                for item in unique_matches:
                    cursor.execute(
                        f"""
                        UPDATE `{CANDIDATE_TABLE}`
                        SET normalized_raw_name=%s,
                            suggested_standard_ingredient_id=%s,
                            suggested_standard_name=%s,
                            model_result_json=%s,
                            status='approved', reviewer='system_exact_match',
                            review_note='标准名/别名归一化后唯一精确匹配', reviewed_at=NOW()
                        WHERE candidate_id=%s
                        """,
                        (
                            item["normalized_raw_name"],
                            item["standard_ingredient_id"],
                            item["standard_name"],
                            json.dumps(item, ensure_ascii=False),
                            item["candidate_id"],
                        ),
                    )
                for item in ambiguous:
                    cursor.execute(
                        f"""
                        UPDATE `{CANDIDATE_TABLE}`
                        SET model_result_json=%s, status='pending', reviewer=NULL,
                            review_note='归一化后对应多个标准原料，需人工确认', reviewed_at=NULL
                        WHERE candidate_id=%s
                        """,
                        (json.dumps(item, ensure_ascii=False), item["candidate_id"]),
                    )
                for item in unmatched:
                    if str(item.get("previous_status") or "").startswith("discarded_"):
                        continue
                    if item.get("previous_status") == "out_of_scope":
                        continue
                    cursor.execute(
                        f"""
                        UPDATE `{CANDIDATE_TABLE}`
                        SET suggested_standard_ingredient_id=NULL,
                            suggested_standard_name=NULL,
                            status='pending', reviewer=NULL, reviewed_at=NULL,
                            review_note='当前标准名/别名表无法唯一匹配'
                        WHERE candidate_id=%s
                        """,
                        (item["candidate_id"],),
                    )
                conn.commit()
            else:
                conn.rollback()

    return {
        "applied": apply,
        "backups": backups,
        "candidate_count": len(candidates),
        "unique_exact_matches": unique_matches,
        "ambiguous_candidates": ambiguous,
        "unmatched_candidate_count": len(unmatched),
        "unmatched_candidates": unmatched,
        "orphan_alias_repairs": orphan_repairs,
        "orphan_alias_deactivations": orphan_deactivations,
        "alias_collision_groups": collision_groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit safe changes; default previews")
    parser.add_argument("--report", type=Path, help="write the full JSON report")
    args = parser.parse_args()
    result = reconcile(apply=bool(args.apply))
    rendered = json.dumps(result, ensure_ascii=False, default=str, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    summary = {key: value for key, value in result.items() if key not in {"unmatched_candidates"}}
    print(json.dumps(summary, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
