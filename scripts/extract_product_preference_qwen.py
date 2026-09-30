#!/usr/bin/env python3
"""Use Qwen to enrich csv_labeling.product_preference_events in place."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pymysql
from openai import OpenAI

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app_config import get_mysql_config, get_qwen_config  # noqa: E402
from comment_pipeline.pipelines.product_preference_pipeline import (  # noqa: E402
    ensure_table,
    save_preference,
)

TABLE_NAME = "product_preference_events"

SYSTEM_PROMPT = r"""
你是宠物用品用户评论结构化抽取助手。严格基于原文抽取，不要补充原文没有表达的信息，不要根据常识推断。

字段定义：
- material: 明确提到的材质数组，如麻绳、棉绳、橡胶、毛绒、塑料、硅胶、羽毛、木头。
- shape: 产品外形/造型的一个简短字符串，没有则 null。
- size: 大小、厚薄、尺寸评价的一个简短字符串，没有则 null。
- function: 产品设计功能数组，如磨牙、发声、自动移动、漏食、弹跳、发光。
- structure: 产品结构组成或设计数组，如麻绳打结、弹簧+羽毛。
- interaction: 宠物与产品的具体互动机制数组，如追逐、扑抓、叼走、拉扯、拍打。
- benefit: 原文明示的正向价值数组，优先使用可聚合短语。
- user_proof: 证明真实使用强度、频率或持续时间的原文事实数组。
- pain_point: 用户明确抱怨的问题数组，没有则 []。
- pet_action: 宠物真实动作数组，如抢、咬、叼、追、扑、闻、拍、拉扯。
- preference: 只能是 positive / negative / mixed / neutral。
- reason: 原文明示的喜欢或不喜欢原因数组。
- durability: 耐用性评价的一个简短字符串。
- evidence_text: 必须完整原样返回输入的评论原文，不得改写。
- confidence: 0-1。

规则：
1. “耐咬”不能自动推断 pet_action=咬 或 interaction=啃咬。
2. “喜欢什么”放 preference，“为什么喜欢”放 reason。
3. “宠物做了什么”放 pet_action，“如何与产品互动”放 interaction。
4. 没有信息就返回 [] 或 null，不要填满字段。
5. 只输出合法 JSON，不输出解释或 Markdown。

输出格式：
{
  "material": [], "shape": null, "size": null, "function": [],
  "structure": [], "interaction": [], "benefit": [], "user_proof": [],
  "pain_point": [], "pet_action": [], "preference": "neutral", "reason": [],
  "durability": null, "evidence_text": "完整原文", "confidence": 0.0
}
""".strip()

ARRAY_FIELDS = (
    "material", "function", "structure", "interaction", "benefit",
    "user_proof", "pain_point", "pet_action", "reason",
)
SCALAR_FIELDS = ("shape", "size", "durability")
PREFERENCES = {"positive", "negative", "mixed", "neutral"}


def clean_json_text(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def _list_value(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def normalize_extraction(data: dict[str, Any], original_text: str) -> dict[str, Any]:
    result: dict[str, Any] = {field: _list_value(data.get(field)) for field in ARRAY_FIELDS}
    for field in SCALAR_FIELDS:
        value = data.get(field)
        if isinstance(value, list):
            value = " / ".join(_list_value(value))
        result[field] = str(value).strip()[:255] if value not in (None, "") else ""
    preference = str(data.get("preference") or "neutral").strip().lower()
    result["preference"] = preference if preference in PREFERENCES else "neutral"
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    result["confidence"] = max(0.0, min(1.0, confidence))
    result["evidence_text"] = original_text
    result["extraction_source"] = "qwen"
    return result


def build_client(config: dict[str, str]) -> OpenAI:
    if not config["api_key"]:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY/QWEN_API_KEY")
    return OpenAI(api_key=config["api_key"], base_url=config["base_url"], timeout=90)


def call_qwen(client: Any, model: str, comment_text: str, max_retries: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"请抽取以下评论：\n\n{comment_text}"},
                ],
            )
            raw = response.choices[0].message.content
            return normalize_extraction(json.loads(clean_json_text(raw)), comment_text)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max_retries:
                time.sleep(min(2 ** (attempt + 1), 8))
    raise RuntimeError(f"抽取失败: {last_error}")


def connect_db():
    return pymysql.connect(
        **get_mysql_config(), autocommit=False, cursorclass=pymysql.cursors.DictCursor,
    )


def load_rows(conn, limit: int = 100, min_id: int = 0, max_id: int | None = None,
              reprocess: bool = False) -> list[dict[str, Any]]:
    conditions = ["id > %s", "evidence_text IS NOT NULL", "TRIM(evidence_text) <> ''"]
    params: list[Any] = [min_id]
    if max_id is not None:
        conditions.append("id <= %s")
        params.append(max_id)
    if not reprocess:
        conditions.append("COALESCE(extraction_source, '') <> 'qwen'")
    sql = (
        f"SELECT id,comment_id,evidence_text,extraction_source FROM `{TABLE_NAME}` "
        f"WHERE {' AND '.join(conditions)} ORDER BY id ASC"
    )
    if limit > 0:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def process_table(limit: int = 100, min_id: int = 0, max_id: int | None = None,
                  reprocess: bool = False, dry_run: bool = False, max_retries: int = 3,
                  sleep_seconds: float = 0.3, client: Any = None) -> dict[str, Any]:
    qwen = get_qwen_config()
    conn = connect_db()
    processed = updated = failed = 0
    errors: list[dict[str, Any]] = []
    try:
        ensure_table(conn, TABLE_NAME)
        rows = load_rows(conn, limit, min_id, max_id, reprocess)
        total = len(rows)
        if total and client is None:
            client = build_client(qwen)
        for index, row in enumerate(rows, start=1):
            print(f"[{index}/{total}] id={row['id']} comment_id={row['comment_id']}")
            try:
                event = call_qwen(client, qwen["model"], str(row["evidence_text"]), max_retries)
                processed += 1
                if not dry_run:
                    save_preference(conn, int(row["comment_id"]), event, TABLE_NAME)
                    updated += 1
            except Exception as exc:
                failed += 1
                errors.append({"id": row["id"], "comment_id": row["comment_id"], "error": str(exc)})
                print("失败:", exc)
            if sleep_seconds > 0 and index < total:
                time.sleep(sleep_seconds)
        return {
            "ok": failed == 0, "table": TABLE_NAME, "model": qwen["model"],
            "selected": total, "processed": processed, "updated": updated,
            "failed": failed, "dry_run": dry_run, "errors": errors[:50],
        }
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100, help="最多处理行数，0 表示全部")
    parser.add_argument("--min-id", type=int, default=0)
    parser.add_argument("--max-id", type=int)
    parser.add_argument("--reprocess", action="store_true", help="重新处理已由 Qwen 抽取的数据")
    parser.add_argument("--dry-run", action="store_true", help="调用模型但不写数据库")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.min_id < 0 or (args.max_id is not None and args.max_id < 0):
        raise SystemExit("limit/min-id/max-id 不能为负数")
    result = process_table(
        args.limit, args.min_id, args.max_id, args.reprocess, args.dry_run,
        max(1, args.max_retries), max(0.0, args.sleep),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
