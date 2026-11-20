"""Read D/M/N and submit collection tasks, then verify task-specific merged row IDs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import openpyxl
import requests

ROOT = "/api/admin/multi-model/"
DETAIL = ROOT + "tasks/{task_id}"
TABLES = {
    "knowledge_point": ("selection", DETAIL, "assembled_points"),
    "health_knowledge_point": ("health", DETAIL, "health_structured.points"),
    "solution_knowledge_point": ("solution", DETAIL, "solution_structured.points"),
    "behavior_knowledge_point": (
        "behavior",
        ROOT + "behavior-summary-points",
        "behavior_summary.items",
    ),
    "daily_care_knowledge_point": (
        "daily_care",
        ROOT + "daily-care-points?view=fused",
        "care_fused.items",
    ),
}


def read_rows(path, sheet=None):
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = book[sheet] if sheet else book.active
        rows = []
        for n, values in enumerate(
            ws.iter_rows(min_row=2, max_col=14, values_only=True), 2
        ):
            question, table, endpoint = (
                str(values[i] or "").strip() for i in (3, 12, 13)
            )
            if not any((question, table, endpoint)):
                continue
            if table not in TABLES or not 1 <= len(question) <= 2000:
                raise ValueError(f"{ws.title} 第 {n} 行 D 或 M 列无效")
            endpoint = endpoint.removeprefix("GET ").strip()
            if endpoint not in (DETAIL, TABLES[table][1]):
                raise ValueError(f"{ws.title} 第 {n} 行 N 列接口与 {table} 不匹配")
            rows.append((n, question, table, endpoint))
        if not rows:
            raise ValueError("工作表没有可导入的问题")
        return rows
    finally:
        book.close()


def verified_ids(call, detail, table, endpoint, task_id):
    base, _, field = TABLES[table]
    points = detail
    for key in field.split("."):
        points = points.get(key, {}) if isinstance(points, dict) else {}
    if not isinstance(points, list):
        return []
    if base == "selection":
        batches = {r["id"] for r in detail["responses"]}
        points = [
            p
            for p in points
            if any(
                source.get("batch_id") in batches
                for entry in p["sources"]
                for source in entry["sources"]
            )
        ]
    ids = {p["id"] for p in points}
    if endpoint != DETAIL and ids:
        page = call("GET", endpoint, params={"task_id": task_id})
        ids.intersection_update(p["id"] for p in page["items"])
    return sorted(ids)


def run(rows, call, ledger, origin, provider, timeout):
    ledger.execute(
        "CREATE TABLE IF NOT EXISTS jobs (key TEXT PRIMARY KEY, task_id INTEGER)"
    )
    bases = {b["code"]: b for b in call("GET", ROOT + "config")["knowledge_bases"]}
    results = []
    for number, question, table, endpoint in rows:
        base = TABLES[table][0]
        key = hashlib.sha256(
            json.dumps([origin, base, question, provider]).encode()
        ).hexdigest()
        record = dict(row=number, table=table)
        try:
            template = bases[base].get("default_template")
            if not template:
                raise ValueError("此知识库尚未启用采集模板")
            with ledger:
                reserved = ledger.execute(
                    "INSERT OR IGNORE INTO jobs VALUES (?,NULL)", (key,)
                ).rowcount
            if reserved:
                try:
                    task_id = call(
                        "POST",
                        ROOT + "tasks",
                        json=dict(
                            question=question,
                            knowledge_base=base,
                            template_code=template,
                            providers=[provider],
                        ),
                    )["task_id"]
                except requests.HTTPError as exc:
                    # Only release a reservation when submission was definitely rejected.
                    if exc.response is not None and exc.response.status_code in (
                        400,
                        401,
                        403,
                        404,
                        409,
                        422,
                        429,
                    ):
                        with ledger:
                            ledger.execute(
                                "DELETE FROM jobs WHERE key=? AND task_id IS NULL",
                                (key,),
                            )
                    raise
                with ledger:
                    ledger.execute(
                        "UPDATE jobs SET task_id=? WHERE key=?", (task_id, key)
                    )
            else:
                task_id = ledger.execute(
                    "SELECT task_id FROM jobs WHERE key=?", (key,)
                ).fetchone()[0]
                if task_id is None:
                    raise ValueError("提交结果不确定，请核对后台任务后恢复任务编号")
            record["task_id"] = task_id
            deadline = time.monotonic() + timeout
            while True:
                detail = call("GET", DETAIL.format(task_id=task_id))
                task = detail["task"]
                if task["question"] != question or task["knowledge_base"] != base:
                    raise ValueError("原问题或知识库不匹配")
                if task["status"] not in ("running", "completed"):
                    raise ValueError("采集失败：" + task["status"])
                if any(r.get("import_error") for r in detail.get("responses", [])):
                    raise ValueError("任务解析入库失败")
                if task["status"] == "completed":
                    ids = verified_ids(call, detail, table, endpoint, task_id)
                    if ids:
                        record.update(status="verified", ids=ids)
                        break
                if time.monotonic() >= deadline:
                    raise TimeoutError("尚未查到与任务关联的目标表记录，可继续运行")
                time.sleep(min(5, max(0, deadline - time.monotonic())))
        except Exception as exc:
            record.update(status="failed", error=str(exc))
        results.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--sheet")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--origin", default="http://127.0.0.1:15386")
    parser.add_argument("--provider", default="qwen")
    parser.add_argument(
        "--ledger", type=Path, default=Path("var/knowledge-init.sqlite")
    )
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    rows = read_rows(args.workbook, args.sheet)
    if not args.apply:
        print(json.dumps({"rows": len(rows), "tables": sorted({r[2] for r in rows})}))
        return 0
    session = requests.Session()
    session.headers["X-Admin-Token"] = os.environ["MINIPROGRAM_ADMIN_TOKEN"]
    origin = args.origin.rstrip("/")

    def call(method, path, **kwargs):
        response = session.request(
            method, origin + path, timeout=60, allow_redirects=False, **kwargs
        )
        response.raise_for_status()
        return response.json()

    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.ledger) as ledger:
        results = run(rows, call, ledger, origin, args.provider, args.timeout)
    return int(any(r["status"] != "verified" for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
