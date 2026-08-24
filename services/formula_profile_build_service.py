"""Build the formula-profile pipeline (module_ranking → structure_labels → formula_profile).

Each step is a standalone CLI script under ``scripts/`` that writes a physical
table in the ``protein_feature_platform`` feature database.  Steps run
sequentially via ``subprocess`` so a ``sys.exit`` in a script cannot tear down
the Flask process, and the pipeline stops at the first failing step.

Step dependency order:
    1. module_ranking   → catfood_module_market_ranking
    2. structure_labels → catfood_formula_structure_labels
    3. formula_profile  → catfood_formula_profile  (depends on 1 + 2)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app_config import get_feature_mysql_config

BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "scripts"

# 顺序固定：module_ranking / structure_labels 是 formula_profile 的前置依赖
PIPELINE_STEPS: list[dict[str, str]] = [
    {
        "key": "module_ranking",
        "name": "市场分位排名",
        "script": "build_module_market_ranking.py",
        "output_table": "catfood_module_market_ranking",
    },
    {
        "key": "structure_labels",
        "name": "配方结构标签",
        "script": "build_formula_structure_labels.py",
        "output_table": "catfood_formula_structure_labels",
    },
    {
        "key": "formula_profile",
        "name": "配方画像",
        "script": "build_formula_profile.py",
        "output_table": "catfood_formula_profile",
    },
]

DEFAULT_TIMEOUT = 1200


def _env() -> dict[str, str]:
    """Pass feature-DB credentials to child scripts via MYSQL_* env vars."""
    cfg = get_feature_mysql_config()
    env = os.environ.copy()
    env.update(
        MYSQL_HOST=str(cfg["host"]),
        MYSQL_PORT=str(cfg["port"]),
        MYSQL_USER=str(cfg["user"]),
        MYSQL_PASSWORD=str(cfg.get("password") or ""),
        MYSQL_DATABASE=str(cfg["database"]),
        MYSQL_CHARSET=str(cfg.get("charset") or "utf8mb4"),
    )
    return env


def _run_step(script: str, env: dict[str, str], timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run one build script as a subprocess and capture its tail log."""
    command = [sys.executable, str(SCRIPT_DIR / script)]
    try:
        proc = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "ok": False,
            "timeout": True,
            "log_tail": (str(exc) or "").splitlines()[-40:],
        }
    log_lines = (proc.stdout or "").splitlines()
    return {
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "timeout": False,
        "log_tail": log_lines[-40:],
    }


def build_formula_profile_pipeline(
    steps: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run the three-step formula-profile build pipeline in order.

    Args:
        steps: step keys to run; defaults to all three in defined order.
            Valid keys: ``module_ranking`` / ``structure_labels`` / ``formula_profile``.
        timeout: per-step subprocess timeout in seconds.

    Returns:
        ``{"ok": bool, "steps": [...]}``; on failure ``failed_step`` is set and
        subsequent steps are skipped.
    """
    selected = steps or [s["key"] for s in PIPELINE_STEPS]
    plan = [s for s in PIPELINE_STEPS if s["key"] in selected]
    if not plan:
        return {"ok": False, "error": "无有效步骤", "steps": []}

    env = _env()
    results: list[dict[str, Any]] = []
    for step in plan:
        run = _run_step(step["script"], env, timeout=timeout)
        results.append({
            "key": step["key"],
            "name": step["name"],
            "script": step["script"],
            "output_table": step["output_table"],
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
