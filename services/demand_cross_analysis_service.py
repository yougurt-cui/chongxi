"""Demand cross-analysis pipeline (病症声量).

Runs ``scripts/build_demand_cross_analysis.py`` as a subprocess to
regenerate ``csv_labeling.catfood_demand_cross_analysis``.

When invoked by the API, the script reads all inputs from the production
server without an SSH tunnel:
  - ``protein_feature_platform.cat_disease_clues``
  - ``csv_labeling.catfood_need_comment_labels``
  - ``csv_labeling.catfood_choice_comments_filtered_v2``
  - production standard brand / product tables

Output granularity: 病症 × (全部 / 年龄段 / 品种).
Write mode: full-replace (DELETE + INSERT) every run.
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

SCRIPT = "build_demand_cross_analysis.py"
OUTPUT_TABLE = "catfood_demand_cross_analysis"

DEFAULT_TIMEOUT = 3600  # SSH 隧道 + 多表 JOIN，默认 1 小时
_pipeline_lock = Lock()


def run_demand_cross_analysis(
    *,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run ``build_demand_cross_analysis.py`` as a subprocess.

    Args:
        dry_run: dry-run mode — the script prints metrics without writing.
        timeout: subprocess timeout in seconds.

    Returns:
        ``{"ok": bool, "log_tail": [...]}``
    """
    if not _pipeline_lock.acquire(blocking=False):
        raise RuntimeError("病症声量任务正在运行，请勿重复提交")
    try:
        # API 运行在生产服务器上：需求标签和病症线索都必须读取线上数据库。
        # 手工在开发机运行脚本时仍可显式选择 ssh 模式。
        cmd = [
            sys.executable,
            str(SCRIPT_DIR / SCRIPT),
            "--clues-connection",
            "direct",
        ]
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
    finally:
        _pipeline_lock.release()
