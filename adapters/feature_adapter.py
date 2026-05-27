"""Feature engineering adapter for the legacy feature-score pipeline."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib import parse, request
from urllib.error import HTTPError, URLError

from sqlalchemy import create_engine, text

from app.app_config import get_feature_mysql_config


FEATURE_PROJECT_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "feature_score_pipeline"
FEATURE_PIPELINE = FEATURE_PROJECT_ROOT / "pipeline.py"

DEFAULT_FEATURE_DB = get_feature_mysql_config()

DEFAULT_TABLES = {
    "protein_source": "protein_source_aggregate",
    "fiber_source": "catfood_fiber_feature_json",
    "fat_source": "catfood_fat_material_features",
}

MATERIAL_SCORE_TABLES = {
    "protein-score": "protein_business_cluster_product_details_scored",
    "fiber-score": "catfood_fiber_feature_score",
    "fat-score": "catfood_fat_material_features_scored",
    "wide": "catfood_protein_fat_fiber_score_wide",
}

RISK_SCORE_TABLES = {
    "sku-feature": "sku_feature_input",
    "black-chin-risk": "sku_risk_score_result",
    "soft-stool-risk": "sku_risk_score_result",
}

RISK_SCORE_MODEL_VERSIONS = {
    "black-chin-risk": "BLACK_CHIN_M2_FAT_OMEGA_FAT_B",
    "soft-stool-risk": "SOFT_STOOL_M2_PQ_NO_G_FAT_B",
}

DEFAULT_FEATURE_API_URL = os.getenv("FEATURE_SCORE_API_URL", "http://127.0.0.1:8765")


def _db_config(payload_db: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = get_feature_mysql_config(payload_db)
    config["port"] = int(config.get("port") or 3306)
    config["charset"] = str(config.get("charset") or "utf8mb4")
    return config


def _pipeline_env(db_config: Dict[str, Any]) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "MYSQL_HOST": str(db_config["host"]),
            "MYSQL_PORT": str(db_config["port"]),
            "MYSQL_USER": str(db_config["user"]),
            "MYSQL_PASSWORD": str(db_config.get("password") or ""),
            "MYSQL_DATABASE": str(db_config["database"]),
            "MYSQL_CHARSET": str(db_config["charset"]),
        }
    )
    return env


def _run_pipeline_command(
    args: Iterable[str],
    *,
    db_config: Dict[str, Any],
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    if not FEATURE_PIPELINE.exists():
        raise FileNotFoundError(f"missing feature pipeline: {FEATURE_PIPELINE}")

    cmd = [sys.executable, str(FEATURE_PIPELINE), *list(args)]
    proc = subprocess.run(
        cmd,
        cwd=str(FEATURE_PROJECT_ROOT),
        env=_pipeline_env(db_config),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
    )
    lines = (proc.stdout or "").splitlines()
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "log_tail": lines[-80:],
    }


def _mysql_url(db_config: Dict[str, Any]) -> str:
    return (
        f"mysql+pymysql://{db_config['user']}:{db_config.get('password') or ''}"
        f"@{db_config['host']}:{db_config['port']}/{db_config['database']}"
        f"?charset={db_config['charset']}"
    )


def count_feature_rows(
    *,
    db_config: Dict[str, Any],
    table_names: Iterable[str],
) -> Dict[str, Optional[int]]:
    engine = create_engine(_mysql_url(db_config), pool_pre_ping=True, future=True)
    counts: Dict[str, Optional[int]] = {}
    try:
        with engine.begin() as conn:
            for table_name in table_names:
                exists = conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM INFORMATION_SCHEMA.TABLES
                        WHERE TABLE_SCHEMA = :schema_name
                          AND TABLE_NAME = :table_name
                        """
                    ),
                    {"schema_name": db_config["database"], "table_name": table_name},
                ).scalar()
                if not exists:
                    counts[table_name] = None
                    continue
                counts[table_name] = int(conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar() or 0)
        return counts
    finally:
        engine.dispose()


def count_risk_score_rows_by_model(
    *,
    db_config: Dict[str, Any],
    table_name: str = "sku_risk_score_result",
) -> Dict[str, int]:
    engine = create_engine(_mysql_url(db_config), pool_pre_ping=True, future=True)
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                    """
                ),
                {"schema_name": db_config["database"], "table_name": table_name},
            ).scalar()
            if not exists:
                return {}

            has_model_version = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                      AND COLUMN_NAME = 'score_model_version'
                    """
                ),
                {"schema_name": db_config["database"], "table_name": table_name},
            ).scalar()
            if not has_model_version:
                return {}

            rows = conn.execute(
                text(
                    f"""
                    SELECT score_model_version, COUNT(*) AS row_count
                    FROM `{table_name}`
                    GROUP BY score_model_version
                    """
                )
            ).all()
            return {str(row[0] or ""): int(row[1] or 0) for row in rows}
    finally:
        engine.dispose()


def _feature_api_url(api_url: Optional[str] = None) -> str:
    return (api_url or DEFAULT_FEATURE_API_URL).rstrip("/")


def _read_feature_api_json(url: str, *, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"feature score API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 8765 特征评分服务：{exc.reason}") from exc

    return json.loads(raw or "{}")


def _submit_feature_task(api_url: str, task: str, extra_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = dict(extra_payload or {})
    payload["task"] = task
    return _read_feature_api_json(
        f"{api_url}/api/run",
        method="POST",
        payload=payload,
    )


def _fetch_feature_job(api_url: str, job_id: str) -> Dict[str, Any]:
    query = parse.urlencode({"id": job_id})
    payload = _read_feature_api_json(f"{api_url}/api/job?{query}")
    return payload.get("job") or {}


def _wait_feature_job(
    api_url: str,
    job_id: str,
    *,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    while True:
        job = _fetch_feature_job(api_url, job_id)
        status = job.get("status")
        if status in {"succeeded", "failed"}:
            return job
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待 8765 job 超时：{job_id}")
        time.sleep(max(0.2, float(poll_interval_seconds)))


def _normalize_material_score_steps(steps: Optional[Iterable[str]]) -> List[str]:
    aliases = {
        "protein": "protein-score",
        "protein_score": "protein-score",
        "protein-score": "protein-score",
        "fiber": "fiber-score",
        "fiber_score": "fiber-score",
        "fiber-score": "fiber-score",
        "fat": "fat-score",
        "fat_score": "fat-score",
        "fat-score": "fat-score",
        "wide": "wide",
        "score_wide": "wide",
    }
    selected = [str(step).strip() for step in (steps or ["protein-score", "fiber-score", "fat-score", "wide"])]
    normalized: List[str] = []
    for step in selected:
        if not step:
            continue
        task = aliases.get(step)
        if task is None:
            raise ValueError(f"unsupported material score step: {step}")
        if task not in normalized:
            normalized.append(task)
    return normalized


def _normalize_risk_score_steps(steps: Optional[Iterable[str]]) -> List[str]:
    aliases = {
        "black": "black-chin-risk",
        "black_chin": "black-chin-risk",
        "black-chin": "black-chin-risk",
        "black_chin_risk": "black-chin-risk",
        "black-chin-risk": "black-chin-risk",
        "soft": "soft-stool-risk",
        "soft_stool": "soft-stool-risk",
        "soft-stool": "soft-stool-risk",
        "soft_stool_risk": "soft-stool-risk",
        "soft-stool-risk": "soft-stool-risk",
    }
    selected = [str(step).strip() for step in (steps or ["black-chin-risk", "soft-stool-risk"])]
    normalized: List[str] = []
    for step in selected:
        if not step:
            continue
        task = aliases.get(step)
        if task is None:
            raise ValueError(f"unsupported risk score step: {step}")
        if task not in normalized:
            normalized.append(task)
    return normalized


def run_risk_score_pipeline(
    *,
    api_url: Optional[str] = None,
    db: Optional[Dict[str, Any]] = None,
    steps: Optional[Iterable[str]] = None,
    wait: bool = False,
    refresh_sku_feature: bool = True,
    timeout_seconds: int = 1800,
    poll_interval_seconds: float = 1.5,
) -> Dict[str, Any]:
    """Run black-chin and soft-stool risk tasks through the vendored local pipeline."""
    db_config = _db_config(db)
    selected_steps = _normalize_risk_score_steps(steps)
    submitted_steps = (["sku-feature"] if refresh_sku_feature else []) + selected_steps
    target_tables = {step: RISK_SCORE_TABLES[step] for step in submitted_steps}
    target_score_model_versions = {
        step: RISK_SCORE_MODEL_VERSIONS[step] for step in selected_steps
    }
    table_names = sorted(set(target_tables.values()))
    before_row_counts = count_feature_rows(
        db_config=db_config,
        table_names=table_names,
    )
    before_score_model_row_counts = count_risk_score_rows_by_model(db_config=db_config)

    results: Dict[str, Any] = {}
    for task in submitted_steps:
        results[task] = _run_pipeline_command(
            [task],
            db_config=db_config,
            timeout_seconds=timeout_seconds,
        )
        if not results[task]["ok"]:
            return {
                "ok": False,
                "mode": "local_pipeline",
                "failed_step": task,
                "steps": selected_steps,
                "submitted_steps": submitted_steps,
                "refresh_sku_feature": bool(refresh_sku_feature),
                "target_tables": target_tables,
                "target_score_model_versions": target_score_model_versions,
                "before_row_counts": before_row_counts,
                "before_score_model_row_counts": before_score_model_row_counts,
                "results": results,
                "db": {k: v for k, v in db_config.items() if k != "password"},
            }

    after_row_counts = count_feature_rows(
        db_config=db_config,
        table_names=table_names,
    )
    row_deltas: Dict[str, Optional[int]] = {}
    new_rows: Dict[str, Optional[int]] = {}
    for table_name in table_names:
        before_count = before_row_counts.get(table_name)
        after_count = after_row_counts.get(table_name)
        if before_count is None or after_count is None:
            row_deltas[table_name] = None
            new_rows[table_name] = None
            continue
        delta = int(after_count) - int(before_count)
        row_deltas[table_name] = delta
        new_rows[table_name] = max(delta, 0)

    after_score_model_row_counts = count_risk_score_rows_by_model(db_config=db_config)
    score_model_row_deltas: Dict[str, int] = {}
    score_model_new_rows: Dict[str, int] = {}
    model_versions = sorted(
        set(before_score_model_row_counts)
        | set(after_score_model_row_counts)
        | set(target_score_model_versions.values())
    )
    for model_version in model_versions:
        before_count = int(before_score_model_row_counts.get(model_version, 0))
        after_count = int(after_score_model_row_counts.get(model_version, 0))
        delta = after_count - before_count
        score_model_row_deltas[model_version] = delta
        score_model_new_rows[model_version] = max(delta, 0)

    return {
        "ok": True,
        "mode": "local_pipeline",
        "steps": selected_steps,
        "submitted_steps": submitted_steps,
        "refresh_sku_feature": bool(refresh_sku_feature),
        "target_tables": target_tables,
        "target_score_model_versions": target_score_model_versions,
        "results": results,
        "before_row_counts": before_row_counts,
        "after_row_counts": after_row_counts,
        "row_deltas": row_deltas,
        "new_rows": new_rows,
        "before_score_model_row_counts": before_score_model_row_counts,
        "after_score_model_row_counts": after_score_model_row_counts,
        "score_model_row_deltas": score_model_row_deltas,
        "score_model_new_rows": score_model_new_rows,
        "row_counts": after_row_counts,
        "db": {k: v for k, v in db_config.items() if k != "password"},
    }


def run_material_score_pipeline(
    *,
    api_url: Optional[str] = None,
    db: Optional[Dict[str, Any]] = None,
    steps: Optional[Iterable[str]] = None,
    wait: bool = False,
    timeout_seconds: int = 1800,
    poll_interval_seconds: float = 1.5,
) -> Dict[str, Any]:
    """Run raw-material score tasks through the vendored local pipeline."""
    db_config = _db_config(db)
    selected_steps = _normalize_material_score_steps(steps)
    target_tables = {step: MATERIAL_SCORE_TABLES[step] for step in selected_steps}
    before_row_counts = count_feature_rows(
        db_config=db_config,
        table_names=target_tables.values(),
    )

    results: Dict[str, Any] = {}
    for task in selected_steps:
        results[task] = _run_pipeline_command(
            [task],
            db_config=db_config,
            timeout_seconds=timeout_seconds,
        )
        if not results[task]["ok"]:
            return {
                "ok": False,
                "mode": "local_pipeline",
                "failed_step": task,
                "steps": selected_steps,
                "target_tables": target_tables,
                "before_row_counts": before_row_counts,
                "results": results,
                "db": {k: v for k, v in db_config.items() if k != "password"},
            }

    after_row_counts = count_feature_rows(
        db_config=db_config,
        table_names=target_tables.values(),
    )
    row_deltas: Dict[str, Optional[int]] = {}
    new_rows: Dict[str, Optional[int]] = {}
    for table_name in target_tables.values():
        before_count = before_row_counts.get(table_name)
        after_count = after_row_counts.get(table_name)
        if before_count is None or after_count is None:
            row_deltas[table_name] = None
            new_rows[table_name] = None
            continue
        delta = int(after_count) - int(before_count)
        row_deltas[table_name] = delta
        new_rows[table_name] = max(delta, 0)

    return {
        "ok": True,
        "mode": "local_pipeline",
        "steps": selected_steps,
        "target_tables": target_tables,
        "results": results,
        "before_row_counts": before_row_counts,
        "after_row_counts": after_row_counts,
        "row_deltas": row_deltas,
        "new_rows": new_rows,
        "row_counts": after_row_counts,
        "db": {k: v for k, v in db_config.items() if k != "password"},
    }


def run_consumer_feature_engineering(
    *,
    db: Optional[Dict[str, Any]] = None,
    steps: Optional[Iterable[str]] = None,
    protein_limit: int = 0,
    protein_concurrency: int = 4,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Run C-side feature engineering source-table builders."""
    db_config = _db_config(db)
    selected_steps = [str(step).strip() for step in (steps or ["protein", "fiber", "fat"]) if str(step).strip()]
    allowed = {"protein", "fiber", "fat"}
    unknown = [step for step in selected_steps if step not in allowed]
    if unknown:
        raise ValueError(f"unsupported steps: {', '.join(unknown)}")

    results: Dict[str, Any] = {}
    if "protein" in selected_steps:
        command = ["protein-source"]
        if protein_limit > 0:
            command.extend(["--limit", str(protein_limit)])
        if protein_concurrency > 0:
            command.extend(["--concurrency", str(protein_concurrency)])
        results["protein"] = _run_pipeline_command(
            command,
            db_config=db_config,
            timeout_seconds=timeout_seconds,
        )
        if not results["protein"]["ok"]:
            return {
                "ok": False,
                "failed_step": "protein",
                "db": {k: v for k, v in db_config.items() if k != "password"},
                "results": results,
            }

    if "fiber" in selected_steps:
        results["fiber"] = _run_pipeline_command(
            ["fiber-extract"],
            db_config=db_config,
            timeout_seconds=timeout_seconds,
        )
        if not results["fiber"]["ok"]:
            return {
                "ok": False,
                "failed_step": "fiber",
                "db": {k: v for k, v in db_config.items() if k != "password"},
                "results": results,
            }

    if "fat" in selected_steps:
        results["fat"] = _run_pipeline_command(
            ["fat-extract"],
            db_config=db_config,
            timeout_seconds=timeout_seconds,
        )
        if not results["fat"]["ok"]:
            return {
                "ok": False,
                "failed_step": "fat",
                "db": {k: v for k, v in db_config.items() if k != "password"},
                "results": results,
            }

    row_counts = count_feature_rows(
        db_config=db_config,
        table_names=DEFAULT_TABLES.values(),
    )
    return {
        "ok": True,
        "mode": "incremental",
        "steps": selected_steps,
        "db": {k: v for k, v in db_config.items() if k != "password"},
        "source_table": "csv_labeling.catfood_ingredient_ocr_parsed",
        "target_tables": DEFAULT_TABLES,
        "row_counts": row_counts,
        "results": results,
    }
