#!/usr/bin/env python
"""导入活动码。

支持两种输入：
1. 纯文本，一行一个码（``--file codes.txt``）；
2. 平台导出的 CSV，自动识别表头并使用其中的积分数 / 兑换截止 / 批次 / 序号
   （``--file pexport_xxx.csv``）。CSV 里若有 ``campaign_name``，且未指定 ``--name``，
   会自动用该名称登记活动。

用法：
    python scripts/import_activity_codes.py --activity-id 1001 --file codes.txt
    python scripts/import_activity_codes.py --activity-id 1001 --file pexport_xxx.csv --dry-run
    python scripts/import_activity_codes.py --activity-id 1001 --name "双十一发帖有礼" --category STOOL
    python scripts/import_activity_codes.py --activity-id 1001 --stats

重复的码自动跳过；同一活动内已存在的码不会被覆盖。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from services.miniprogram_activity_service import (  # noqa: E402
    get_activity_stats,
    import_activity_code_records,
    upsert_activity,
)

# 导出 CSV 列名 → 入库字段
CSV_FIELD_MAP = {
    "code": "code",
    "campaign_name": "campaign_name",
    "credits_per_code": "credits_per_code",
    "redeem_ends_at": "redeem_ends_at",
    "batch_id": "batch",
    "sequence_no": "sequence_no",
    "status": "source_status",
    "snapshot_at": "snapshot_at",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入/查看宠析活动码")
    parser.add_argument("--activity-id", type=int, required=True, help="活动 ID（正整数）")
    parser.add_argument("--file", help="码文件：纯文本（一行一个码）或平台导出的 CSV")
    parser.add_argument("--batch", default="", help="批次标记（CSV 内的 batch_id 优先）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--stats", action="store_true", help="仅查看库存统计")
    parser.add_argument("--name", help="登记或更新活动名称（CSV 未给时取 campaign_name）")
    parser.add_argument("--category", default="", help="限定帖子分类，如 STOOL / FUNNY")
    parser.add_argument("--status", default="active", help="活动状态：active / paused / closed")
    parser.add_argument("--starts-at", help="开始时间，格式 YYYY-MM-DD HH:MM:SS（UTC）")
    parser.add_argument("--ends-at", help="结束时间，格式 YYYY-MM-DD HH:MM:SS（UTC）")
    return parser


def _read_csv(path: Path) -> tuple[list[dict], dict]:
    """读取导出 CSV，返回 (记录列表, 元信息)。"""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [(name or "").strip().lower() for name in (reader.fieldnames or [])]
        if "code" not in fieldnames:
            raise ValueError("CSV 缺少 code 列")
        rows: list[dict] = []
        meta: dict = {}
        for raw in reader:
            row = {(key or "").strip().lower(): (value or "").strip() for key, value in raw.items()}
            record = {target: row[source] for source, target in CSV_FIELD_MAP.items() if row.get(source)}
            if not record.get("code"):
                continue
            meta.setdefault("campaign_name", record.get("campaign_name") or "")
            rows.append(record)
    if rows:
        batches = {row.get("batch") for row in rows if row.get("batch")}
        meta["batch"] = batches.pop() if len(batches) == 1 else ""
    return rows, meta


def _read_text(path: Path) -> tuple[list[str], dict]:
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()], {}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    meta: dict = {}

    if args.file:
        path = Path(args.file).expanduser()
        if not path.exists():
            print(f"文件不存在：{path}", file=sys.stderr)
            return 2
        is_csv = path.suffix.lower() == ".csv" or path.read_text(encoding="utf-8-sig").split("\n", 1)[0].lower().startswith("code,")
        records, meta = _read_csv(path) if is_csv else _read_text(path)
        print(f"输入：{'CSV' if is_csv else '纯文本'}，共 {len(records)} 条")

    activity_name = args.name or meta.get("campaign_name")
    if activity_name:
        if args.dry_run:
            print(f"[dry-run] 将登记活动 {args.activity_id}：{activity_name}（未写库）")
        else:
            upsert_activity(
                activity_id=args.activity_id,
                name=activity_name,
                status=args.status,
                required_category_code=args.category,
                starts_at=args.starts_at,
                ends_at=args.ends_at,
            )
            print(f"活动 {args.activity_id} 已登记/更新：{activity_name}")

    if args.file:
        result = import_activity_code_records(
            activity_id=args.activity_id,
            records=records,
            batch=args.batch or meta.get("batch") or "",
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.stats or (not args.file and not activity_name):
        print(json.dumps(get_activity_stats(args.activity_id), ensure_ascii=False, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
