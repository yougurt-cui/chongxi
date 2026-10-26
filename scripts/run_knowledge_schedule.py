#!/usr/bin/env python3
"""Run the two-stage production job once; scheduling belongs to the OS timer."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services import knowledge_schedule_service as job  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--max-posts', type=int, default=500)
    parser.add_argument('--comment-batches', type=int, default=4)
    parser.add_argument('--chunk', type=int, default=20)
    parser.add_argument('--retries', type=int, default=1)
    parser.add_argument('--model')
    args = parser.parse_args()
    if args.status:
        print(json.dumps(job.recent_runs(), ensure_ascii=False))
        return 0
    directory = Path(os.environ.get('KNOWLEDGE_JOB_STATE_DIR', ROOT / 'var'))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'knowledge-production.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'skipped_locked'}))
            return 0
        try:
            result = job.run(max_posts=args.max_posts, comment_batches=args.comment_batches,
                             chunk=args.chunk, retries=args.retries, model=args.model)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 1 if result.get('failures') else 0
        except Exception as exc:
            print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}), flush=True)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
