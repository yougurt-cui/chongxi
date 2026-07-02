#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified entrypoint for the protein/fiber/fat score pipeline.

This project intentionally reuses the existing production scripts in
``scripts/``. The commands here add orchestration and simple source-table
imports without changing the scoring logic in those scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd
from sqlalchemy import Numeric, create_engine, text


PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = PROJECT_ROOT / "scripts"
APP_DIR = PROJECT_ROOT / "apps"
CSV_LABELING_PROJECT = Path(
    os.getenv(
        "CSV_LABELING_PROJECT",
        "/home/admin/projects/chongxi/vendor/csv_mysql_labeling",
    )
)
CSV_LABELING_PYTHON = Path(
    os.getenv(
        "CSV_LABELING_PYTHON",
        "/home/admin/projects/chongxi/.venv/bin/python",
    )
)

DEFAULT_DB = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "protein_feature_platform"),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
}

TABLES = {
    "protein_source": "protein_source_aggregate",
    "protein_score": "protein_business_cluster_product_details_scored",
    "fiber_source": "catfood_fiber_feature_json",
    "fiber_score": "catfood_fiber_feature_score",
    "fat_source": "catfood_fat_material_features",
    "fat_score": "catfood_fat_material_features_scored",
    "wide": "catfood_protein_fat_fiber_score_wide",
    "sku_feature": "sku_feature_input",
    "sku_pathway_signal": "sku_pathway_signal",
    "sku_symptom_mechanism_signal": "sku_symptom_mechanism_signal",
    "sku_process_observation_signal": "sku_process_observation_signal",
    "sku_risk": "sku_risk_score_result",
}


def mysql_url(database: str | None = None) -> str:
    db = database or DEFAULT_DB["database"]
    return (
        f"mysql+pymysql://{DEFAULT_DB['user']}:{DEFAULT_DB['password']}"
        f"@{DEFAULT_DB['host']}:{DEFAULT_DB['port']}/{db}"
        f"?charset={DEFAULT_DB['charset']}"
    )


def get_engine(database: str | None = None):
    return create_engine(mysql_url(database), pool_pre_ping=True)


def run_script(script_name: str, args: Iterable[str] = ()) -> None:
    script_path = SCRIPT_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"missing script: {script_path}")

    cmd = [sys.executable, str(script_path), *list(args)]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def rebuild_catfood_protein_labels() -> None:
    if not CSV_LABELING_PROJECT.exists():
        raise FileNotFoundError(f"missing csv labeling project: {CSV_LABELING_PROJECT}")
    python_bin = str(CSV_LABELING_PYTHON if CSV_LABELING_PYTHON.exists() else Path(sys.executable))

    cmd = [
        python_bin,
        "-m",
        "src.cli",
        "catfood-label-engineering",
        "--source-table",
        "catfood_ingredient_ocr_parsed",
        "--protein-table",
        "catfood_feature_protein_labels",
        "--limit",
        "0",
        "--concurrency",
        os.getenv("CATFOOD_PROTEIN_LABEL_CONCURRENCY", "4"),
    ]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(CSV_LABELING_PROJECT), check=True)


def read_input_file(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    if suffix in {".jsonl", ".ndjson"}:
        rows = []
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no} invalid JSONL") from exc
        return pd.DataFrame(rows)
    if suffix == ".json":
        return pd.read_json(path)

    raise ValueError(f"unsupported input file type: {path.suffix}")


def normalize_json_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for col in result.columns:
        if not (col.endswith("_json") or col in {"ingredient_feature_json", "source_ids"}):
            continue
        result[col] = result[col].apply(
            lambda value: json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else value
        )
    return result


def import_table(path: Path, table: str, if_exists: str) -> None:
    if if_exists not in {"append", "replace", "fail"}:
        raise ValueError("--if-exists must be append, replace, or fail")

    df = normalize_json_columns(read_input_file(path))
    if df.empty:
        raise ValueError(f"input file has no rows: {path}")

    engine = get_engine()
    numeric_types = {
        col: Numeric(12, 4)
        for col in df.columns
        if pd.api.types.is_numeric_dtype(df[col])
    }
    with engine.begin() as conn:
        df.to_sql(table, conn, if_exists=if_exists, index=False, dtype=numeric_types)

    print(f"imported {len(df)} rows into {DEFAULT_DB['database']}.{table}")


def command_protein_source(args) -> None:
    script_path = PROJECT_ROOT.parents[1] / "scripts" / "rebuild_protein_source_from_profiles.py"
    script_args = []
    if not args.dry_run:
        script_args.append("--apply")
    if args.keep_backup:
        script_args.append("--keep-backup")
    cmd = [sys.executable, str(script_path), *script_args]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT.parents[1]), check=True)


def command_protein_score(args) -> None:
    env = os.environ.copy()
    env.setdefault("PROTEIN_SCORE_SOURCE_TABLE", TABLES["protein_source"])
    env.setdefault("PROTEIN_SCORE_OUTPUT_TABLE", TABLES["protein_score"])
    env.setdefault("PROTEIN_SCORE_IF_EXISTS", args.if_exists)
    cmd = [sys.executable, str(SCRIPT_DIR / "protein_score1.py")]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True, env=env)


def command_fiber_import(args) -> None:
    import_table(Path(args.input), TABLES["fiber_source"], args.if_exists)


def command_fiber_extract(args) -> None:
    run_script("fiber_remark.py")


def command_fiber_score(args) -> None:
    run_script("fiber_remark_score.py")


def command_fat_import(args) -> None:
    import_table(Path(args.input), TABLES["fat_source"], args.if_exists)


def command_fat_extract(args) -> None:
    run_script("fat_material_remark.py")


def command_fat_score(args) -> None:
    run_script("fat_score1.py")


def command_wide(args) -> None:
    run_script("build_catfood_score_wide_table.py")


def command_sku_feature(args) -> None:
    run_script("build_sku_feature_input.py")


def command_sku_pathway_signal(args) -> None:
    cmd = [sys.executable, str(PROJECT_ROOT.parent / "sku_pathway_signal.py")]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT.parent), check=True)


def command_sku_symptom_mechanism_signal(args) -> None:
    cmd = [sys.executable, str(PROJECT_ROOT.parent / "sku_symptom_mechanism_signal.py")]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT.parent), check=True)


def command_sku_process_observation_signal(args) -> None:
    cmd = [sys.executable, str(PROJECT_ROOT.parent / "sku_process_observation_signal.py")]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT.parent), check=True)


def command_product_display(args) -> None:
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_DIR / "product_display.py"),
        "--server.address",
        args.host,
        "--server.port",
        str(args.port),
    ]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def command_product_compare(args) -> None:
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_DIR / "product_compare_qwen.py"),
        "--server.address",
        args.host,
        "--server.port",
        str(args.port),
    ]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def command_b2b_order_analysis(args) -> None:
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_DIR / "b2b_order_analysis_app.py"),
        "--server.address",
        args.host,
        "--server.port",
        str(args.port),
    ]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def command_product_clue_analysis(args) -> None:
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP_DIR / "product_clue_analysis_app.py"),
        "--server.address",
        args.host,
        "--server.port",
        str(args.port),
    ]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def command_black_chin_risk(args) -> None:
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "black_risk_done.py"),
        "--batch_id",
        args.batch_id,
        "--feature_version",
        args.feature_version,
        "--no_reference_monitor",
    ]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def command_soft_stool_risk(args) -> None:
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "soft_risk_done.py"),
        "--batch_id",
        args.batch_id,
        "--feature_version",
        args.feature_version,
        "--no_reference_monitor",
    ]
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)


def command_all(args) -> None:
    command_protein_source(args)
    command_protein_score(args)
    if args.extract_fiber:
        command_fiber_extract(args)
    command_fiber_score(args)
    if args.extract_fat:
        command_fat_extract(args)
    command_fat_score(args)
    command_wide(args)


def command_status(args) -> None:
    engine = get_engine()
    with engine.connect() as conn:
        for label, table in TABLES.items():
            exists = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = :schema_name
                      AND TABLE_NAME = :table_name
                    """
                ),
                {"schema_name": DEFAULT_DB["database"], "table_name": table},
            ).scalar()
            if not exists:
                print(f"{label:14s} {table:45s} missing")
                continue
            count = conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
            print(f"{label:14s} {table:45s} {count}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run catfood feature score pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("protein-source", help="write protein_source_aggregate")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--keep-backup", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="max new parsed OCR rows to process, 0 means all")
    p.add_argument("--concurrency", type=int, default=0, help="parallel LLM requests, 0 uses script default")
    p.set_defaults(func=command_protein_source)

    p = sub.add_parser("protein-score", help="write protein_business_cluster_product_details_scored")
    p.add_argument("--if-exists", choices=["replace", "append", "fail"], default="replace")
    p.set_defaults(func=command_protein_score)

    p = sub.add_parser("fiber-import", help="import rows into catfood_fiber_feature_json")
    p.add_argument("input", help="CSV/XLSX/JSON/JSONL file")
    p.add_argument("--if-exists", choices=["append", "replace", "fail"], default="append")
    p.set_defaults(func=command_fiber_import)

    p = sub.add_parser("fiber-extract", help="write catfood_fiber_feature_json using existing extractor")
    p.set_defaults(func=command_fiber_extract)

    p = sub.add_parser("fiber-score", help="write catfood_fiber_feature_score")
    p.set_defaults(func=command_fiber_score)

    p = sub.add_parser("fat-import", help="import rows into catfood_fat_material_features")
    p.add_argument("input", help="CSV/XLSX/JSON/JSONL file")
    p.add_argument("--if-exists", choices=["append", "replace", "fail"], default="append")
    p.set_defaults(func=command_fat_import)

    p = sub.add_parser("fat-extract", help="write catfood_fat_material_features using existing extractor")
    p.set_defaults(func=command_fat_extract)

    p = sub.add_parser("fat-score", help="write catfood_fat_material_features_scored")
    p.set_defaults(func=command_fat_score)

    p = sub.add_parser("wide", help="write catfood_protein_fat_fiber_score_wide")
    p.set_defaults(func=command_wide)

    p = sub.add_parser("sku-feature", help="write sku_feature_input for black chin and soft stool")
    p.set_defaults(func=command_sku_feature)

    p = sub.add_parser("sku-pathway-signal", help="write sku_pathway_signal from protein/fiber/fat source tables")
    p.set_defaults(func=command_sku_pathway_signal)

    p = sub.add_parser("sku-symptom-mechanism-signal", help="write sku_symptom_mechanism_signal from risk tags and main pathways")
    p.set_defaults(func=command_sku_symptom_mechanism_signal)

    p = sub.add_parser("sku-process-observation-signal", help="write sku_process_observation_signal from symptom mechanism signals")
    p.set_defaults(func=command_sku_process_observation_signal)

    p = sub.add_parser("product-display", help="run Streamlit product display app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8501")
    p.set_defaults(func=command_product_display)

    p = sub.add_parser("product-compare", help="run Streamlit product compare app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8502")
    p.set_defaults(func=command_product_compare)

    p = sub.add_parser("b2b-order-analysis", help="run Streamlit B2B order analysis app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8503")
    p.set_defaults(func=command_b2b_order_analysis)

    p = sub.add_parser("product-clue-analysis", help="run Streamlit product clue attribution analysis app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8503")
    p.set_defaults(func=command_product_clue_analysis)

    p = sub.add_parser("black-chin-risk", help="write black chin rows into sku_risk_score_result")
    p.add_argument("--feature-version", default="v1")
    p.add_argument("--batch-id", default="current")
    p.set_defaults(func=command_black_chin_risk)

    p = sub.add_parser("soft-stool-risk", help="write soft stool rows into sku_risk_score_result")
    p.add_argument("--feature-version", default="soft_v1")
    p.add_argument("--batch-id", default="current")
    p.set_defaults(func=command_soft_stool_risk)

    p = sub.add_parser("all", help="run protein, fiber, fat scoring and wide table")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--keep-backup", action="store_true")
    p.add_argument("--if-exists", choices=["replace", "append", "fail"], default="append")
    p.add_argument("--extract-fiber", action="store_true", help="also rebuild fiber source table")
    p.add_argument("--extract-fat", action="store_true", help="also rebuild fat source table")
    p.set_defaults(func=command_all)

    p = sub.add_parser("status", help="show row counts for managed tables")
    p.set_defaults(func=command_status)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
