"""Comment ingestion adapter for Douyin/Xiaohongshu catfood candidates."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine


VENDOR_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "csv_mysql_labeling"

try:
    from app.vendor.csv_mysql_labeling.scripts import generate_douyin_raw_comments_sql as douyin_loader
    from app.vendor.csv_mysql_labeling.src.db import make_engine
    from app.vendor.csv_mysql_labeling.src.extract_catfood import run_catfood_extraction_incremental
    from app.vendor.csv_mysql_labeling.src.ingest_xhs import DEFAULT_ARCHIVE_DIR, ingest_xhs_csv
    from app.vendor.csv_mysql_labeling.src.settings import load_settings
except ModuleNotFoundError:
    from vendor.csv_mysql_labeling.scripts import generate_douyin_raw_comments_sql as douyin_loader
    from vendor.csv_mysql_labeling.src.db import make_engine
    from vendor.csv_mysql_labeling.src.extract_catfood import run_catfood_extraction_incremental
    from vendor.csv_mysql_labeling.src.ingest_xhs import DEFAULT_ARCHIVE_DIR, ingest_xhs_csv
    from vendor.csv_mysql_labeling.src.settings import load_settings


DATA_ROOT = Path(os.getenv("CHONGXI_DATA_ROOT", "/home/admin/data/chongxi"))
DEFAULT_DOUYIN_DIR = DATA_ROOT / "douyin_csv"
DEFAULT_XHS_DIR = DATA_ROOT / "xhs_csv"
DEFAULT_DOUYIN_TABLE = "douyin_raw_comments"
DEFAULT_XHS_TABLE = "xiaohongshu_raw_comments"
DEFAULT_TARGET_TABLE = "catfood_brand_health_candidates"
DEFAULT_STATE_TABLE = "catfood_brand_health_extract_state"
DEFAULT_DOUYIN_ARCHIVE_DIR = DATA_ROOT / "archive" / "douyin"
TABLE_RE = re.compile(r"^[A-Za-z0-9_]+$")


def load_default_db_config() -> Dict[str, Any]:
    return dict(load_settings().mysql)


def _safe_table(name: str) -> str:
    if not TABLE_RE.fullmatch(name or ""):
        raise ValueError(f"Invalid table name: {name}")
    return name


def _table_exists(conn: Any, table_name: str) -> bool:
    return bool(
        conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).scalar()
    )


def _index_exists(conn: Any, table_name: str, index_name: str) -> bool:
    return bool(
        conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.statistics
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                  AND index_name = :index_name
                """
            ),
            {"table_name": table_name, "index_name": index_name},
        ).scalar()
    )


def _fetch_columns(conn: Any, table_name: str) -> set[str]:
    rows = conn.execute(
        text(
            """
            SELECT COLUMN_NAME
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
            """
        ),
        {"table_name": table_name},
    ).fetchall()
    return {row[0] for row in rows}


def ensure_douyin_raw_comments_table(engine: Engine, table_name: str = DEFAULT_DOUYIN_TABLE) -> None:
    table_name = _safe_table(table_name)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS `{table_name}` (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  external_id CHAR(32) NOT NULL,
                  post_title TEXT NULL,
                  post_content LONGTEXT NULL,
                  post_like_count INT NULL,
                  comment_text LONGTEXT NOT NULL,
                  comment_date DATE NULL,
                  search_keyword VARCHAR(255) NULL,
                  comment_ip VARCHAR(64) NULL,
                  source_file VARCHAR(255) NULL,
                  ingest_ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE KEY uq_external_id (external_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        )


def ensure_xhs_raw_comments_table(engine: Engine, table_name: str = DEFAULT_XHS_TABLE) -> None:
    table_name = _safe_table(table_name)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS `{table_name}` (
                  id BIGINT PRIMARY KEY AUTO_INCREMENT,
                  source_name VARCHAR(64) NOT NULL,
                  external_id VARCHAR(128) NOT NULL,
                  title TEXT NULL,
                  content LONGTEXT NULL,
                  comment_text LONGTEXT NOT NULL,
                  comment_time VARCHAR(64) NULL,
                  created_at VARCHAR(64) NULL,
                  like_count INT NULL,
                  location VARCHAR(128) NULL,
                  query_keyword VARCHAR(255) NULL,
                  ingest_batch_id VARCHAR(64) NOT NULL,
                  ingest_ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  label_status ENUM('PENDING','DONE','ERROR') NOT NULL DEFAULT 'PENDING',
                  label_ts DATETIME NULL,
                  UNIQUE KEY uq_external_id (external_id),
                  KEY idx_ingest_ts (ingest_ts),
                  KEY idx_query_keyword (query_keyword),
                  KEY idx_label_status (label_status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        )


def _unique_archive_path(path: Path) -> Path:
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def archive_files(files: Iterable[Path], archive_dir: Path) -> List[Dict[str, str]]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived: List[Dict[str, str]] = []
    for file_path in files:
        if not file_path.exists() or not file_path.is_file():
            continue
        target = _unique_archive_path(archive_dir / file_path.name)
        shutil.move(str(file_path), str(target))
        archived.append({"source": str(file_path), "archived_path": str(target)})
    return archived


def dedupe_table_by_external_id(engine: Engine, table_name: str) -> Dict[str, Any]:
    table_name = _safe_table(table_name)
    with engine.begin() as conn:
        if not _table_exists(conn, table_name):
            return {"table_name": table_name, "exists": False, "deleted_rows": 0, "unique_index": False}

        cols = _fetch_columns(conn, table_name)
        if "id" not in cols or "external_id" not in cols:
            return {"table_name": table_name, "exists": True, "deleted_rows": 0, "unique_index": False}

        result = conn.execute(
            text(
                f"""
                DELETE t
                FROM `{table_name}` t
                INNER JOIN (
                  SELECT external_id, MIN(id) AS keep_id
                  FROM `{table_name}`
                  WHERE external_id IS NOT NULL AND TRIM(external_id) <> ''
                  GROUP BY external_id
                  HAVING COUNT(*) > 1
                ) d ON t.external_id = d.external_id AND t.id <> d.keep_id
                """
            )
        )
        deleted_rows = int(result.rowcount or 0)

        unique_index = _index_exists(conn, table_name, "uq_external_id")
        if not unique_index:
            conn.execute(text(f"ALTER TABLE `{table_name}` ADD UNIQUE KEY uq_external_id (external_id)"))
            unique_index = True

    return {
        "table_name": table_name,
        "exists": True,
        "deleted_rows": deleted_rows,
        "unique_index": unique_index,
    }


def ingest_douyin_comments(
    *,
    engine: Engine,
    data_dir: Path,
    table_name: str = DEFAULT_DOUYIN_TABLE,
    batch_size: int = 1000,
    archive_dir: Path = DEFAULT_DOUYIN_ARCHIVE_DIR,
    move_success_files: bool = True,
) -> Dict[str, Any]:
    table_name = _safe_table(table_name)
    if not data_dir.exists():
        raise FileNotFoundError(f"抖音数据目录不存在: {data_dir}")
    if not data_dir.is_dir():
        raise NotADirectoryError(f"抖音数据路径不是目录: {data_dir}")

    ensure_douyin_raw_comments_table(engine, table_name=table_name)
    csv_files = sorted(data_dir.glob("search_comments_*.csv"))
    records = douyin_loader.load_records(data_dir=data_dir)
    if not records:
        return {
            "table_name": table_name,
            "data_dir": str(data_dir),
            "prepared_rows": 0,
            "inserted_rows": 0,
            "archive_dir": str(archive_dir) if move_success_files else None,
            "archived_files": 0,
        }

    insert_sql = text(
        f"""
        INSERT IGNORE INTO `{table_name}` (
          external_id, post_title, post_content, post_like_count, comment_text,
          comment_date, search_keyword, comment_ip, source_file, ingest_ts
        )
        VALUES (
          :external_id, :post_title, :post_content, :post_like_count, :comment_text,
          :comment_date, :search_keyword, :comment_ip, :source_file, NOW()
        )
        """
    )

    inserted_rows = 0
    with engine.begin() as conn:
        for start in range(0, len(records), batch_size):
            chunk = records[start : start + batch_size]
            result = conn.execute(insert_sql, chunk)
            inserted_rows += int(result.rowcount or 0)

    archived = archive_files(csv_files, archive_dir) if move_success_files else []
    return {
        "table_name": table_name,
        "data_dir": str(data_dir),
        "prepared_rows": len(records),
        "inserted_rows": inserted_rows,
        "archive_dir": str(archive_dir) if move_success_files else None,
        "archived_files": len(archived),
        "archive_samples": archived[:20],
    }


def _iter_xhs_csv_files(data_dir: Path, pattern: str) -> List[Path]:
    if not data_dir.exists():
        raise FileNotFoundError(f"小红书数据目录不存在: {data_dir}")
    if not data_dir.is_dir():
        raise NotADirectoryError(f"小红书数据路径不是目录: {data_dir}")
    archive_dir = DEFAULT_ARCHIVE_DIR.resolve()
    files = []
    for path in sorted(data_dir.glob(pattern)):
        if not path.is_file():
            continue
        try:
            path.resolve().relative_to(archive_dir)
            continue
        except ValueError:
            files.append(path)
    return files


def ingest_xhs_comments(
    *,
    engine: Engine,
    data_dir: Path,
    table_name: str = DEFAULT_XHS_TABLE,
    pattern: str = "*.csv",
    source_name: str = "xiaohongshu",
    fallback_keyword: Optional[str] = None,
    encoding: str = "utf-8-sig",
    batch_size: int = 2000,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
) -> Dict[str, Any]:
    table_name = _safe_table(table_name)
    if table_name != DEFAULT_XHS_TABLE:
        raise ValueError("legacy xhs ingestor currently writes to xiaohongshu_raw_comments only")

    ensure_xhs_raw_comments_table(engine, table_name=table_name)
    files = _iter_xhs_csv_files(data_dir=data_dir, pattern=pattern)
    file_results: List[Dict[str, Any]] = []
    total_rows = 0
    for csv_path in files:
        rows, batch_id = ingest_xhs_csv(
            engine=engine,
            csv_path=csv_path,
            source_name=source_name,
            fallback_keyword=fallback_keyword,
            encoding=encoding,
            batch_size=batch_size,
            archive_dir=archive_dir,
        )
        total_rows += rows
        file_results.append({"file": csv_path.name, "rows": rows, "batch_id": batch_id})

    return {
        "table_name": table_name,
        "data_dir": str(data_dir),
        "pattern": pattern,
        "files": len(files),
        "ingested_rows": total_rows,
        "file_results": file_results[:20],
    }


def _serialize_extract_result(result: Any) -> Dict[str, Any]:
    return {
        "batch_id": result.batch_id,
        "inserted_rows": result.inserted_rows,
        "scanned_douyin_rows": result.scanned_douyin_rows,
        "scanned_xhs_rows": result.scanned_xhs_rows,
        "current_douyin_max_ts": (
            result.current_douyin_max_ts.isoformat(sep=" ") if result.current_douyin_max_ts else None
        ),
        "current_xhs_max_ts": result.current_xhs_max_ts.isoformat(sep=" ") if result.current_xhs_max_ts else None,
    }


def collect_comment_data(
    *,
    db: Optional[Dict[str, Any]] = None,
    steps: Optional[Iterable[str]] = None,
    douyin_dir: Path = DEFAULT_DOUYIN_DIR,
    xhs_dir: Path = DEFAULT_XHS_DIR,
    douyin_table: str = DEFAULT_DOUYIN_TABLE,
    xhs_table: str = DEFAULT_XHS_TABLE,
    target_table: str = DEFAULT_TARGET_TABLE,
    state_table: str = DEFAULT_STATE_TABLE,
    xhs_pattern: str = "*.csv",
    xhs_source_name: str = "xiaohongshu",
    xhs_fallback_keyword: Optional[str] = None,
    xhs_encoding: str = "utf-8-sig",
    batch_size: int = 2000,
    douyin_archive_dir: Path = DEFAULT_DOUYIN_ARCHIVE_DIR,
    move_douyin_success_files: bool = True,
) -> Dict[str, Any]:
    db_config = load_default_db_config()
    db_config.update(db or {})
    db_config["port"] = int(db_config.get("port") or 3306)
    db_config["charset"] = str(db_config.get("charset") or "utf8mb4")

    selected_steps = [str(step).strip() for step in (steps or ["douyin", "xhs", "dedupe", "extract"]) if str(step).strip()]
    allowed = {"douyin", "xhs", "dedupe", "extract"}
    unknown = [step for step in selected_steps if step not in allowed]
    if unknown:
        raise ValueError(f"unsupported steps: {', '.join(unknown)}")

    engine = make_engine(db_config)
    results: Dict[str, Any] = {}
    try:
        if "douyin" in selected_steps:
            results["douyin"] = ingest_douyin_comments(
                engine=engine,
                data_dir=douyin_dir,
                table_name=douyin_table,
                batch_size=batch_size,
                archive_dir=douyin_archive_dir,
                move_success_files=move_douyin_success_files,
            )

        if "xhs" in selected_steps:
            results["xhs"] = ingest_xhs_comments(
                engine=engine,
                data_dir=xhs_dir,
                table_name=xhs_table,
                pattern=xhs_pattern,
                source_name=xhs_source_name,
                fallback_keyword=xhs_fallback_keyword,
                encoding=xhs_encoding,
                batch_size=batch_size,
            )

        if "dedupe" in selected_steps:
            results["dedupe"] = {
                "douyin": dedupe_table_by_external_id(engine, douyin_table),
                "xhs": dedupe_table_by_external_id(engine, xhs_table),
            }

        if "extract" in selected_steps:
            extract_result = run_catfood_extraction_incremental(
                engine=engine,
                target_table=target_table,
                state_table=state_table,
            )
            results["extract"] = _serialize_extract_result(extract_result)

        return {
            "ok": True,
            "mode": "incremental",
            "steps": selected_steps,
            "db": {k: v for k, v in db_config.items() if k != "password"},
            "source_dirs": {"douyin": str(douyin_dir), "xiaohongshu": str(xhs_dir)},
            "raw_tables": {"douyin": douyin_table, "xiaohongshu": xhs_table},
            "target_table": target_table,
            "state_table": state_table,
            "results": results,
        }
    finally:
        engine.dispose()
