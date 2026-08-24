"""Disease representative pipeline (病症代表产品与代表原料).

Runs ``scripts/build_formula_ingredient_disease.py`` as a subprocess to
regenerate three tables in ``csv_labeling``:
  - ``catfood_formula_ingredient_disease``      (明细桥表)
  - ``catfood_disease_representative_product``  (代表产品)
  - ``catfood_disease_representative_ingredient`` (代表原料)

When invoked by the API, the script reads cat_disease_clues directly from
the production feature database without an SSH tunnel. Manual runs on a
development machine can still use the default ``ssh`` mode.

Write mode: atomic swap (create _next → insert → RENAME).
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

SCRIPT = "build_formula_ingredient_disease.py"
OUTPUT_TABLES = [
    "catfood_formula_ingredient_disease",
    "catfood_disease_representative_product",
    "catfood_disease_representative_ingredient",
]

DEFAULT_TIMEOUT = 3600  # 远程线索读取 + 多表 JOIN + 原子换表
_pipeline_lock = Lock()


def run_disease_representative(
    *,
    dry_run: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run ``build_formula_ingredient_disease.py`` as a subprocess.

    Args:
        dry_run: dry-run mode — the script prints metrics without writing.
        timeout: subprocess timeout in seconds.

    Returns:
        ``{"ok": bool, "log_tail": [...]}``
    """
    if not _pipeline_lock.acquire(blocking=False):
        raise RuntimeError("病症代表产品任务正在运行，请勿重复提交")
    try:
        # API 运行在生产服务器上：直接连线上特征库，不走 SSH 隧道。
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
