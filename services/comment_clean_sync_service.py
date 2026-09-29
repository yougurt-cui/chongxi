"""Unified comment routing and remote sync pipeline.

Two-step pipeline:
    1. Pipeline — run ``comment_pipeline.run_pipeline`` to clean raw comments,
                  route them, and execute Choice / Product Preference pipelines.
    2. Sync     — sync the shared clean layer, router results, and both pipeline
                  outputs to the remote server in dependency order.

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
PIPELINE_MODULE = "comment_pipeline.run_pipeline"
OUTPUT_TABLES = [
    "comment_clean_base",
    "comment_router_result",
    "catfood_choice_comments_filtered_v2",
    "product_preference_events",
]

DEFAULT_TIMEOUT = 1800  # cleaning can take a while for large comment sets


def _run_clean(
    dry_run: bool = False,
    limit: int = 0,
    reprocess: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run the unified comment pipeline as a subprocess."""
    cmd = [
        sys.executable,
        "-m", PIPELINE_MODULE,
    ]
    if dry_run:
        cmd.append("--dry-run")
    if limit and limit > 0:
        cmd.extend(["--limit", str(limit)])
    if reprocess:
        cmd.append("--reprocess")

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
    reprocess: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run the shared-clean → route → two-pipeline → sync workflow.

    Args:
        dry_run:     both steps run in dry-run mode (no writes, no remote inserts).
        limit:       debug limit per source table for the cleaning step; 0 = all.
        skip_clean:  skip the cleaning step, only sync to remote.
        skip_sync:   skip the sync step, only run cleaning.
        reprocess:   reroute existing clean comments and refresh outputs.
        timeout:     per-step subprocess timeout for the cleaning script.

    Returns:
        ``{"ok": bool, "clean": {...}, "sync": {...}}``; on failure
        ``failed_step`` is set and subsequent steps are skipped.
    """
    steps: dict[str, Any] = {}

    # Step 1: Clean
    if not skip_clean:
        clean_result = _run_clean(
            dry_run=dry_run, limit=limit, reprocess=reprocess, timeout=timeout,
        )
        steps["clean"] = {
            "module": PIPELINE_MODULE,
            "output_tables": OUTPUT_TABLES,
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
                tables=OUTPUT_TABLES,
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
