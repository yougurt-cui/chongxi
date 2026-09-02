"""Incrementally materialize one standardized cat-food formula."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pymysql

from app_config import get_feature_mysql_config, get_mysql_config
from scripts.backfill_formula_ingredient_features import (
    backfill_formula,
    materialize_formula_science_source_tables,
)


BASE_DIR = Path(__file__).resolve().parents[1]
FEATURE_DIR = BASE_DIR / "vendor" / "feature_score_pipeline"
SCRIPT_DIR = FEATURE_DIR / "scripts"


def _science_source_statuses(formula_id: int) -> dict[str, str | None]:
    tables = {
        "protein": "protein_source_aggregate",
        "fat": "catfood_fat_material_features",
        "fiber": "catfood_fiber_feature_json",
    }
    statuses: dict[str, str | None] = {}
    with pymysql.connect(
        **get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor
    ) as conn:
        with conn.cursor() as cursor:
            for domain, table in tables.items():
                cursor.execute(
                    f"SELECT profile_status FROM `{table}` WHERE formula_id=%s",
                    (int(formula_id),),
                )
                row = cursor.fetchone()
                statuses[domain] = row.get("profile_status") if row else None
    return statuses


def build_formula_profile(*, formula_id: int, apply: bool = True) -> dict[str, Any]:
    """Standardize ingredient items and rebuild the formula gate profile."""
    result = backfill_formula(
        int(formula_id), apply=apply, materialize_science_features=False
    )
    with pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM catfood_formula_feature_profile WHERE formula_id=%s",
                (int(formula_id),),
            )
            profile = cursor.fetchone()
    if not profile:
        raise RuntimeError(f"formula profile 未生成: {formula_id}")
    return {**result, "profile": profile, "overall_status": profile["overall_status"]}


def _env(formula_id: int, batch_id: str) -> dict[str, str]:
    cfg = get_feature_mysql_config()
    env = os.environ.copy()
    env.update(
        MYSQL_HOST=str(cfg["host"]),
        MYSQL_PORT=str(cfg["port"]),
        MYSQL_USER=str(cfg["user"]),
        MYSQL_PASSWORD=str(cfg.get("password") or ""),
        MYSQL_DATABASE=str(cfg["database"]),
        MYSQL_CHARSET=str(cfg.get("charset") or "utf8mb4"),
        FORMULA_ID=str(int(formula_id)),
        SKU_FEATURE_BATCH_ID=batch_id,
        PROTEIN_SCORE_IF_EXISTS="append",
        FAT_SCORE_IF_EXISTS="append",
        WIDE_SCORE_IF_EXISTS="append",
    )
    return env


def _run(command: list[str], *, env: dict[str, str], cwd: Path = BASE_DIR) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=600,
    )
    result = {"ok": proc.returncode == 0, "returncode": proc.returncode, "log_tail": (proc.stdout or "").splitlines()[-40:]}
    if proc.returncode:
        raise RuntimeError("\n".join(result["log_tail"]))
    return result


def materialize_formula_scores(*, formula_id: int, batch_id: str | None = None) -> dict[str, Any]:
    formula_id = int(formula_id)
    batch_id = batch_id or f"formula-{formula_id}"
    with pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT overall_status FROM catfood_formula_feature_profile WHERE formula_id=%s",
                (formula_id,),
            )
            profile = cursor.fetchone()
    if not profile or profile["overall_status"] != "ready_for_rebuild":
        return {"ok": False, "status": "need_review", "formula_id": formula_id, "overall_status": profile and profile["overall_status"]}

    source_statuses = _science_source_statuses(formula_id)
    if any(status != "ready" for status in source_statuses.values()):
        return {
            "ok": False,
            "status": "science_features_not_ready",
            "formula_id": formula_id,
            "source_statuses": source_statuses,
        }

    env = _env(formula_id, batch_id)
    with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM catfood_fat_material_features_scored WHERE formula_id=%s", (formula_id,))
        conn.commit()
    steps = {
        "protein_score": _run([sys.executable, str(SCRIPT_DIR / "protein_score1.py")], env=env, cwd=FEATURE_DIR),
        "fiber_score": _run([sys.executable, str(SCRIPT_DIR / "fiber_remark_score.py")], env=env, cwd=FEATURE_DIR),
        "fat_score": _run([sys.executable, str(SCRIPT_DIR / "fat_score1.py")], env=env, cwd=FEATURE_DIR),
        "wide": _run([sys.executable, str(SCRIPT_DIR / "build_catfood_score_wide_table.py")], env=env, cwd=FEATURE_DIR),
        "sku_feature": _run([sys.executable, str(SCRIPT_DIR / "build_sku_feature_input.py")], env=env, cwd=FEATURE_DIR),
    }
    # The target table is keyed by formula/version, so an incremental upsert can
    # reuse an older row.  Persist this run's batch explicitly: the following
    # risk node selects its input by batch_id.
    with pymysql.connect(
        **get_feature_mysql_config(), autocommit=False
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sku_feature_input
                SET batch_id=%s
                WHERE formula_id=%s AND feature_version IN ('v1', 'soft_v1')
                """,
                (batch_id, formula_id),
            )
        conn.commit()
    return {"ok": True, "formula_id": formula_id, "batch_id": batch_id, "steps": steps}


def materialize_formula_science_features(
    *, formula_id: int, batch_id: str | None = None
) -> dict[str, Any]:
    """Materialize the three layer-4 source tables from active science profiles."""
    formula_id = int(formula_id)
    batch_id = batch_id or f"formula-{formula_id}"
    with pymysql.connect(**get_mysql_config(), cursorclass=pymysql.cursors.DictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT overall_status FROM catfood_formula_feature_profile WHERE formula_id=%s",
                (formula_id,),
            )
            profile = cursor.fetchone()
    if not profile or profile["overall_status"] != "ready_for_rebuild":
        return {
            "ok": False,
            "status": "need_review",
            "formula_id": formula_id,
            "overall_status": profile and profile["overall_status"],
        }

    env = _env(formula_id, batch_id)
    source_env = dict(env)
    source_env["MYSQL_DATABASE"] = str(get_mysql_config()["database"])
    steps = {
        "protein_source": _run(
            [sys.executable, str(BASE_DIR / "scripts/rebuild_protein_source_from_profiles.py"),
             "--apply", "--formula-id", str(formula_id)],
            env=source_env,
        ),
    }
    source_tables = materialize_formula_science_source_tables(formula_id, apply=True)
    steps["fat_source"] = {
        "ok": source_tables["fat_profile_status"] == "ready",
        "profile_status": source_tables["fat_profile_status"],
        "missing_science_count": source_tables["fat_missing_science_count"],
    }
    steps["fiber_source"] = {
        "ok": source_tables["fiber_profile_status"] == "ready",
        "profile_status": source_tables["fiber_profile_status"],
        "missing_science_count": source_tables["fiber_missing_science_count"],
    }
    source_statuses = _science_source_statuses(formula_id)
    ready = all(status == "ready" for status in source_statuses.values())
    return {
        "ok": ready,
        "status": "materialized" if ready else "need_review",
        "formula_id": formula_id,
        "batch_id": batch_id,
        "source_tables": {
            "protein": "protein_source_aggregate",
            "fat": "catfood_fat_material_features",
            "fiber": "catfood_fiber_feature_json",
        },
        "source_statuses": source_statuses,
        "steps": steps,
    }


def materialize_formula_risks(*, formula_id: int, batch_id: str | None = None) -> dict[str, Any]:
    formula_id = int(formula_id)
    batch_id = batch_id or f"formula-{formula_id}"
    env = _env(formula_id, batch_id)
    with pymysql.connect(**get_feature_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM sku_risk_score_result
                WHERE formula_id=%s AND batch_id=%s
                  AND score_model_version IN (%s,%s)
                """,
                (formula_id, batch_id, "BLACK_CHIN_M2_FAT_OMEGA_FAT_B", "SOFT_STOOL_M2_PQ_NO_G_FAT_B"),
            )
        conn.commit()
    steps = {
        "black_chin": _run([sys.executable, str(FEATURE_DIR / "black_risk_done.py"), "--batch_id", batch_id, "--feature_version", "v1", "--no_reference_monitor"], env=env, cwd=FEATURE_DIR),
        "soft_stool": _run([sys.executable, str(FEATURE_DIR / "soft_risk_done.py"), "--batch_id", batch_id, "--feature_version", "soft_v1", "--no_reference_monitor"], env=env, cwd=FEATURE_DIR),
    }
    return {"ok": True, "formula_id": formula_id, "batch_id": batch_id, "steps": steps}
