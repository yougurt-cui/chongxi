#!/usr/bin/env python3
"""按 topic/subtopic 批量运行知识库二次蒸馏，可安全重复执行。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import knowledge_second_stage_service as svc  # noqa: E402


def emit(event: str, **payload) -> None:
    print(json.dumps({"time": datetime.now().isoformat(timespec="seconds"),
                      "event": event, **payload}, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("llm", "demo"), default="llm")
    parser.add_argument("--chunk", type=int, default=20)
    parser.add_argument("--model")
    parser.add_argument("--topic")
    parser.add_argument("--subtopic")
    args = parser.parse_args()

    engine = svc._engine()
    placeholders = ",".join(f":status_{i}" for i in range(len(svc.ELIGIBLE_SOURCE_STATUSES)))
    where = [f"status IN ({placeholders})"]
    params = {f"status_{i}": value for i, value in enumerate(svc.ELIGIBLE_SOURCE_STATUSES)}
    if args.topic:
        where.append("topic=:topic")
        params["topic"] = args.topic
    if args.subtopic:
        where.append("subtopic=:subtopic")
        params["subtopic"] = args.subtopic
    sql = ("SELECT topic,subtopic,COUNT(*) n FROM knowledge_base WHERE "
           + " AND ".join(where)
           + " GROUP BY topic,subtopic ORDER BY n,topic,subtopic")
    with engine.connect() as conn:
        groups = [dict(row) for row in conn.execute(text(sql), params).mappings()]

    emit("batch_start", groups=len(groups), mode=args.mode, model=args.model, chunk=args.chunk)
    failures = 0
    for index, group in enumerate(groups, 1):
        identity = {"index": index, "groups": len(groups), "topic": group["topic"],
                    "subtopic": group["subtopic"], "source_rows": group["n"]}
        emit("group_start", **identity)
        try:
            result = svc.run_distill(group["topic"], group["subtopic"], mode=args.mode,
                                     chunk=args.chunk, model=args.model)
            emit("group_done", **identity, result=result)
        except Exception as exc:  # continue other independent groups
            failures += 1
            emit("group_failed", **identity, error=f"{type(exc).__name__}: {exc}")
    emit("batch_done", groups=len(groups), failures=failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
