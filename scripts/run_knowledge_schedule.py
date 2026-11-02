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
    parser.add_argument('--comment-batches', type=int, default=1)
    parser.add_argument('--comment-sample-cap', type=int, default=50)
    parser.add_argument('--max-llm-calls', type=int, default=300)
    parser.add_argument('--max-llm-tokens', type=int, default=1_000_000)
    parser.add_argument('--verify', action='store_true',
                        help='Run costly cross-post evidence verification (off by default).')
    parser.add_argument('--chunk', type=int, default=20)
    parser.add_argument('--retries', type=int, default=1)
    parser.add_argument('--model', default=os.getenv('KNOWLEDGE_LLM_MODEL', 'qwen3.7-flash'))
    parser.add_argument(
        '--force-reprocess', action='store_true',
        help='Explicitly rerun post extraction even when source content is unchanged (may be costly).',
    )
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
                             chunk=args.chunk, retries=args.retries, model=args.model,
                             force_reprocess=args.force_reprocess,
                             comment_sample_cap=args.comment_sample_cap,
                             max_llm_calls=args.max_llm_calls,
                             max_llm_tokens=args.max_llm_tokens,
                             verify=args.verify)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 1 if result.get('failures') else 0
        except Exception as exc:
            print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}), flush=True)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
