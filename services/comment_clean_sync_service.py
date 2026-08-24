"""Comment cleaning and remote sync pipeline.

Two-step pipeline:
    1. Clean — run ``filter_catfood_choice_comments.py`` as a subprocess to
               scan raw comment tables (xiaohongshu / douyin) and append matching
               choice comments into ``catfood_choice_comments_filtered_v2``.
    2. Sync  — call ``db_sync_service.sync_tables()`` to push the local
               ``catfood_choice_comments_filtered_v2`` to the remote server
               (8.130.170.148) via SSH tunnel, append-only by business key.

Steps run sequentially; a failure in step 1 skips step 2.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from services.db_sync_service import sync_tables

BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "scripts"

CLEAN_SCRIPT = "filter_catfood_choice_comments.py"
OUTPUT_TABLE = "catfood_choice_comments_filtered_v2"

DEFAULT_TIMEOUT = 1800  # cleaning can take a while for large comment sets


def _run_clean(
    dry_run: bool = False,
    limit: int = 0,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run the comment filtering script as a subprocess."""
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / CLEAN_SCRIPT),
        "--output-table", OUTPUT_TABLE,
    ]
    if dry_run:
        cmd.append("--dry-run")
    if limit and limit > 0:
        cmd.extend(["--limit", str(limit)])

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


def clean_and_sync_comments(
    dry_run: bool = False,
    limit: int = 0,
    skip_clean: bool = False,
    skip_sync: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run the clean → sync pipeline for ``catfood_choice_comments_filtered_v2``.

    Args:
        dry_run:     both steps run in dry-run mode (no writes, no remote inserts).
        limit:       debug limit per source table for the cleaning step; 0 = all.
        skip_clean:  skip the cleaning step, only sync to remote.
        skip_sync:   skip the sync step, only run cleaning.
        timeout:     per-step subprocess timeout for the cleaning script.

    Returns:
        ``{"ok": bool, "clean": {...}, "sync": {...}}``; on failure
        ``failed_step`` is set and subsequent steps are skipped.
    """
    steps: dict[str, Any] = {}

    # Step 1: Clean
    if not skip_clean:
        clean_result = _run_clean(dry_run=dry_run, limit=limit, timeout=timeout)
        steps["clean"] = {
            "script": CLEAN_SCRIPT,
            "output_table": OUTPUT_TABLE,
            "dry_run": dry_run,
            **clean_result,
        }
        if not clean_result["ok"]:
            return {
                "ok": False,
                "error": "清洗步骤执行失败",
                "failed_step": "clean",
                **steps,
            }

    # Step 2: Sync to remote (8.130.170.148)
    if not skip_sync:
        try:
            sync_result = sync_tables(
                tables=[OUTPUT_TABLE],
                dry_run=dry_run,
            )
            steps["sync"] = {
                "remote_host": "8.130.170.148",
                "dry_run": dry_run,
                **sync_result,
            }
            if not sync_result.get("ok"):
                return {
                    "ok": False,
                    "error": "上传同步步骤执行失败",
                    "failed_step": "sync",
                    **steps,
                }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"上传同步异常: {exc}",
                "failed_step": "sync",
                **steps,
            }

    return {"ok": True, **steps}
