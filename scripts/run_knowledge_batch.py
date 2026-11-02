#!/usr/bin/env python3
"""Submit or synchronize durable Model Studio Batch comment jobs."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services import knowledge_batch_service as batch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("submit", "sync", "run"))
    parser.add_argument("--model", default="qwen3.7-flash")
    parser.add_argument("--max-requests", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--per-post-cap", type=int, default=50,
                        help="0 explicitly processes all pending historical comments")
    args = parser.parse_args()
    if args.action == "sync":
        result = batch.sync(model=args.model)
    else:
        options = dict(model=args.model, max_requests=args.max_requests,
                       batch_size=args.batch_size, per_post_cap=args.per_post_cap)
        result = batch.submit(**options) if args.action == "submit" else batch.run(**options)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
