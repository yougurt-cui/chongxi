"""Comment mining pipeline (need → decision → switch → experience labelers).

Each step is a standalone CLI script under ``scripts/`` that reads from
``catfood_choice_comments_filtered_v2`` (filtered by ``intent_labels``)
and writes to the corresponding comment label table.  Steps run
sequentially via ``subprocess``.

Step list:
    1. need_labeler        → catfood_need_comment_labels         (FIND_IN_SET('Need'))
    2. decision_labeler    → catfood_decision_comment_labels     (FIND_IN_SET('Decision'))
    3. switch_labeler      → catfood_switch_comment_labels       (FIND_IN_SET('Switch'))
    4. experience_labeler  → catfood_experience_comment_labels   (FIND_IN_SET('Experience'))

Incremental: each script pre-loads existing ``content_hash`` from the
target table and skips already-labeled comments, so re-running only
processes new (unlabeled) rows.  ``ON DUPLICATE KEY UPDATE`` provides
SQL-level idempotency for all four tables.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from threading import Lock
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "scripts"

# 顺序固定：4 个 labeler 之间无依赖关系，但按 intent_labels 分流顺序执行
PIPELINE_STEPS: list[dict[str, str]] = [
    {
        "key": "need",
        "name": "需求标签",
        "script": "need_comment_labeler.py",
        "output_table": "catfood_need_comment_labels",
        "intent": "Need",
    },
    {
        "key": "decision",
        "name": "决策标签",
        "script": "decision_comment_labeler.py",
        "output_table": "catfood_decision_comment_labels",
        "intent": "Decision",
    },
    {
        "key": "switch",
        "name": "切换标签",
        "script": "switch_comment_labeler.py",
        "output_table": "catfood_switch_comment_labels",
        "intent": "Switch",
    },
    {
        "key": "experience",
        "name": "体验标签",
        "script": "experience_comment_labeler.py",
        "output_table": "catfood_experience_comment_labels",
        "intent": "Experience",
    },
]

DEFAULT_TIMEOUT = 3600  # 每个脚本默认超时 1 小时（大表扫描可能较慢）
_pipeline_lock = Lock()


def _run_step(
    script: str,
    *,
    limit: int = 0,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run one labeler script as a subprocess and capture its tail log."""
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / script),
        "--database",
    ]
    if limit and limit > 0:
        cmd.extend(["--limit", str(limit)])
    if dry_run:
        cmd.append("--dry-run")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "timeout": True,
            "returncode": None,
            "log_tail": (str(exc) or "").splitlines()[-40:],
        }

    log_lines = (proc.stdout or "").splitlines()
    return {
        "ok": proc.returncode == 0,
        "timeout": False,
        "returncode": proc.returncode,
        "log_tail": log_lines[-40:],
    }


def run_comment_mining_pipeline(
    *,
    steps: list[str] | None = None,
    limit: int = 0,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run the four labeler scripts in order.

    Args:
        steps:   step keys to run; defaults to all four in defined order.
                 Valid keys: ``need`` / ``decision`` / ``switch`` / ``experience``.
        limit:   max source rows per script; 0 = no limit (full scan).
        dry_run: dry-run mode — scripts count and report without writing.
        timeout: per-step subprocess timeout in seconds.

    Returns:
        ``{"ok": bool, "steps": [...]}``; on failure ``failed_step`` is set
        and subsequent steps are skipped.
    """
    valid_keys = {s["key"] for s in PIPELINE_STEPS}
    selected = steps or [s["key"] for s in PIPELINE_STEPS]
    unknown = [key for key in selected if key not in valid_keys]
    if unknown:
        raise ValueError(f"未知步骤: {', '.join(unknown)}")
    plan = [s for s in PIPELINE_STEPS if s["key"] in selected]
    if not plan:
        return {"ok": False, "error": "无有效步骤", "steps": []}

    if not _pipeline_lock.acquire(blocking=False):
        raise RuntimeError("评论数据挖掘任务正在运行，请勿重复提交")
    try:
        results: list[dict[str, Any]] = []
        for step in plan:
            run = _run_step(
                step["script"],
                limit=limit,
                dry_run=dry_run,
                timeout=timeout,
            )
            results.append({
                "key": step["key"],
                "name": step["name"],
                "script": step["script"],
                "output_table": step["output_table"],
                "intent": step["intent"],
                **run,
            })
            if not run["ok"]:
                return {
                    "ok": False,
                    "error": f"步骤 {step['name']}（{step['script']}）执行失败",
                    "failed_step": step["key"],
                    "steps": results,
                }

        return {"ok": True, "steps": results}
    finally:
        _pipeline_lock.release()
