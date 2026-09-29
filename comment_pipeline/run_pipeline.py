"""Run shared cleaning, multi-label routing, and both comment pipelines."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from comment_pipeline.common.cleaner import clean_source_row
from comment_pipeline.common.db import (
    connect,
    get_route,
    init_tables,
    load_brand_aliases,
    load_product_aliases,
    upsert_clean_comment,
    upsert_route,
)
from comment_pipeline.pipelines.choice_pipeline import ensure_choice_table, extract_choice, save_choice
from comment_pipeline.pipelines.product_preference_pipeline import (
    ensure_table as ensure_preference_table,
    extract_preference,
    save_preference,
)
from comment_pipeline.router import RouteResult, route_comment
from scripts.filter_catfood_choice_comments import (
    ARTIFACT_ROOT,
    SOURCE_SPECS,
    SOURCE_TABLES,
    iter_source_rows,
    quote_ident,
    write_csv,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="每个来源表最多读取数量，0表示全部")
    parser.add_argument("--min-id", type=int, default=-1, help="只读取源表 id 大于此值的评论")
    parser.add_argument(
        "--output-table", default="catfood_choice_comments_filtered_v2",
        help="猫粮Choice Pipeline输出表",
    )
    parser.add_argument("--output-dir", default=str(ARTIFACT_ROOT), help="本轮 CSV 和摘要目录")
    parser.add_argument("--no-llm", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--reprocess", action="store_true", help="重新路由并覆盖已处理评论")
    parser.add_argument("--dry-run", action="store_true", help="执行清洗和路由但不建表、不写数据库")
    return parser.parse_args()


def _write_preference_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "comment_id", "material", "shape", "size", "function", "structure",
        "interaction", "benefit", "user_proof", "pain_point", "pet_action", "preference", "reason", "durability",
        "evidence_text", "confidence", "extraction_source",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value
                for key, value in row.items()
            })


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_id = "comment_pipeline_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    run_dir = Path(args.output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    conn = connect()
    summary: dict[str, Any] = {
        "run_id": run_id, "dry_run": bool(args.dry_run), "sources": {},
        "cleaned": 0, "filtered_empty_or_noise": 0, "deduplicated_or_existing": 0,
        "choice_routed": 0, "product_preference_routed": 0, "router_skipped_existing": 0,
        "choice_skipped_historical": 0, "preference_skipped_historical": 0,
        "preference_skipped_low_information": 0,
    }
    choice_rows: list[dict[str, Any]] = []
    preference_rows: list[dict[str, Any]] = []
    try:
        brand_aliases = load_brand_aliases(conn)
        product_aliases = load_product_aliases(conn)
        if not args.dry_run:
            init_tables(conn)
            ensure_choice_table(conn, args.output_table)
            ensure_preference_table(conn)
        historical_choice_keys: set[tuple[str, str, str]] = set()
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT source_platform,source_table,source_record_key FROM {quote_ident(args.output_table)}"
            )
            historical_choice_keys = {
                (row["source_platform"], row["source_table"], row["source_record_key"])
                for row in cursor.fetchall()
            }
            cursor.execute("SELECT comment_id FROM product_preference_events")
            historical_preference_ids = {int(row["comment_id"]) for row in cursor.fetchall()}
        for table in SOURCE_TABLES:
            spec = SOURCE_SPECS[table]
            source_stats = {"scanned": 0, "cleaned": 0, "choice": 0, "product_preference": 0}
            for raw_row in iter_source_rows(spec, args.min_id, args.limit):
                source_stats["scanned"] += 1
                comment = clean_source_row(spec, raw_row, brand_aliases, product_aliases)
                if comment is None:
                    summary["filtered_empty_or_noise"] += 1
                    continue
                summary["cleaned"] += 1
                source_stats["cleaned"] += 1
                comment_id = 0
                created = True
                if not args.dry_run:
                    comment_id, created = upsert_clean_comment(conn, comment)
                    if not created:
                        summary["deduplicated_or_existing"] += 1
                    existing_route = get_route(conn, comment_id)
                    if existing_route and not args.reprocess:
                        summary["router_skipped_existing"] += 1
                        route = RouteResult(
                            bool(existing_route["choice_match"]),
                            bool(existing_route["product_preference_match"]),
                            str(existing_route["router_source"]),
                        )
                    else:
                        route = route_comment(comment)
                        upsert_route(conn, comment_id, route)
                else:
                    route = route_comment(comment)
                # Deliberately use two independent if blocks: a comment may enter both.
                if route.choice:
                    summary["choice_routed"] += 1
                    source_stats["choice"] += 1
                    choice_key = (comment.platform, comment.source_table, comment.source_id)
                    if choice_key in historical_choice_keys and not args.reprocess:
                        summary["choice_skipped_historical"] += 1
                    else:
                        choice_row = extract_choice(comment, run_id)
                        choice_rows.append(choice_row)
                        if not args.dry_run:
                            save_choice(conn, args.output_table, choice_row)
                if route.product_preference:
                    summary["product_preference_routed"] += 1
                    source_stats["product_preference"] += 1
                    if comment_id in historical_preference_ids and not args.reprocess:
                        summary["preference_skipped_historical"] += 1
                    else:
                        event = extract_preference(comment)
                        if event is None:
                            summary["preference_skipped_low_information"] += 1
                        else:
                            event["comment_id"] = comment_id
                            preference_rows.append(event)
                            if not args.dry_run:
                                save_preference(conn, comment_id, event)
            summary["sources"][table] = source_stats
        choice_csv = run_dir / "catfood_choice_comments.csv"
        preference_csv = run_dir / "product_preference_events.csv"
        write_csv(choice_csv, choice_rows)
        _write_preference_csv(preference_csv, preference_rows)
        summary.update({
            "choice_csv": str(choice_csv),
            "product_preference_csv": str(preference_csv),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        (run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return summary
    finally:
        conn.close()


def main() -> int:
    summary = run(parse_args())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
