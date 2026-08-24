"""Incremental database sync service.

Syncs local MySQL tables to the remote server via SSH tunnel.
The sync is append-only and key-based: each batch checks which business keys
already exist on the remote server, then inserts only missing rows. This covers
both normal new rows and historical backfills whose timestamps are older than
the remote table's current maximum timestamp.
"""

import logging
import os
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional

import pymysql
from sqlalchemy import create_engine, text

from app_config import get_mysql_config

logger = logging.getLogger(__name__)

# ---- SSH tunnel config ----
_SSH_HOST = os.getenv("DB_SYNC_SSH_HOST", "8.130.170.148")
_SSH_PORT = int(os.getenv("DB_SYNC_SSH_PORT", "22"))
_SSH_USER = os.getenv("DB_SYNC_SSH_USER", "root")
_SSH_KEY_PATH = os.getenv(
    "DB_SYNC_SSH_KEY",
    str(__import__("pathlib").Path.home() / ".ssh" / "id_rsa"),
)

# ---- Remote MySQL (accessed through tunnel) ----
_REMOTE_MYSQL_USER = os.getenv("DB_SYNC_REMOTE_MYSQL_USER", "root")
_REMOTE_MYSQL_PASSWORD = os.getenv(
    "DB_SYNC_REMOTE_MYSQL_PASSWORD", os.getenv("MYSQL_PASSWORD", "")
)
_REMOTE_MYSQL_DB = os.getenv("DB_SYNC_REMOTE_MYSQL_DATABASE", "csv_labeling")

# ---- Table sync specifications ----
SYNC_TABLES = {
    "douyin_raw_comments": {
        "watermark_col": "ingest_ts",
        "key_cols": ["external_id"],
        "select_cols": [
            "external_id", "post_title", "post_content", "post_like_count",
            "comment_text", "comment_date", "search_keyword", "comment_ip",
            "source_file", "ingest_ts",
        ],
        "insert_cols": [
            "external_id", "post_title", "post_content", "post_like_count",
            "comment_text", "comment_date", "search_keyword", "comment_ip",
            "source_file", "ingest_ts",
        ],
    },
    "xiaohongshu_raw_comments": {
        "watermark_col": "ingest_ts",
        "key_cols": ["external_id"],
        "select_cols": [
            "source_name", "external_id", "title", "content",
            "comment_text", "comment_time", "created_at", "like_count",
            "location", "query_keyword", "ingest_batch_id", "ingest_ts",
            "label_status", "label_ts",
        ],
        "insert_cols": [
            "source_name", "external_id", "title", "content",
            "comment_text", "comment_time", "created_at", "like_count",
            "location", "query_keyword", "ingest_batch_id", "ingest_ts",
            "label_status", "label_ts",
        ],
    },
    "catfood_choice_comments_filtered_v2": {
        "watermark_col": "inserted_at",
        "key_cols": ["source_platform", "external_id"],
        "select_cols": [
            "source_platform", "source_schema", "source_table", "source_row_id",
            "external_id", "source_record_key", "comment_text", "normalized_text",
            "intent_labels", "matched_signals", "choice_score",
            "mentions_brand", "mentions_condition", "condition_confidence",
            "condition_categories", "condition_symptoms", "condition_keywords",
            "source_title", "source_content", "source_like_count",
            "source_comment_time", "source_keyword", "inserted_at",
        ],
        "insert_cols": [
            "source_platform", "source_schema", "source_table", "source_row_id",
            "external_id", "source_record_key", "comment_text", "normalized_text",
            "intent_labels", "matched_signals", "choice_score",
            "mentions_brand", "mentions_condition", "condition_confidence",
            "condition_categories", "condition_symptoms", "condition_keywords",
            "source_title", "source_content", "source_like_count",
            "source_comment_time", "source_keyword", "inserted_at",
        ],
    },
}

BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# SSH tunnel helper (subprocess-based, no extra dependencies)
# ---------------------------------------------------------------------------

class _SSHTunnel:
    """SSH port-forward tunnel using subprocess ``ssh -L``."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self.local_port: Optional[int] = None

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def open(self):
        self.local_port = self._find_free_port()
        cmd = [
            "ssh",
            "-i", _SSH_KEY_PATH,
            "-p", str(_SSH_PORT),
            "-o", "StrictHostKeyChecking=no",
            "-o", "ServerAliveInterval=30",
            "-N", "-L",
            f"{self.local_port}:127.0.0.1:3306",
            f"{_SSH_USER}@{_SSH_HOST}",
        ]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Wait for the tunnel to be ready
        for _ in range(50):  # up to 5 seconds
            time.sleep(0.1)
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=1):
                    break
            except (ConnectionRefusedError, OSError):
                continue
        else:
            self.close()
            raise RuntimeError("SSH tunnel did not open in time")
        logger.info("SSH tunnel opened on local port %s -> %s:3306",
                     self.local_port, _SSH_HOST)

    def close(self):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
            logger.info("SSH tunnel closed")


def _get_local_engine():
    """Create a SQLAlchemy engine for the local database."""
    cfg = get_mysql_config()
    url = (
        f"mysql+pymysql://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['database']}"
        f"?charset={cfg.get('charset', 'utf8mb4')}"
    )
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600, future=True)


def _get_remote_connection(local_port: int):
    """Create a pymysql connection to the remote DB through the SSH tunnel."""
    return pymysql.connect(
        host="127.0.0.1",
        port=local_port,
        user=_REMOTE_MYSQL_USER,
        password=_REMOTE_MYSQL_PASSWORD,
        database=_REMOTE_MYSQL_DB,
        charset="utf8mb4",
        connect_timeout=30,
        read_timeout=120,
        write_timeout=120,
    )


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def _get_remote_watermark(remote_conn, table: str, wm_col: str) -> Optional[str]:
    """Get the MAX(watermark_col) from the remote table."""
    cur = remote_conn.cursor()
    cur.execute(f"SELECT MAX(`{wm_col}`) FROM `{table}`")
    val = cur.fetchone()[0]
    cur.close()
    if val is None:
        return None
    return str(val)


def _count_local_table_rows(local_engine, table: str) -> int:
    """Count local rows for reporting; table names come from SYNC_TABLES."""
    with local_engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar() or 0)


def _row_key(row: tuple, select_cols: List[str], key_cols: List[str]) -> tuple:
    return tuple(row[select_cols.index(col)] for col in key_cols)


def _fetch_existing_remote_keys(
    remote_conn,
    table: str,
    key_cols: List[str],
    keys: List[tuple],
) -> set:
    """Fetch keys already present on the remote table for one local batch."""
    if not keys:
        return set()

    unique_keys = list(dict.fromkeys(keys))
    key_select = ", ".join(f"`{col}`" for col in key_cols)
    where_parts = []
    params = []
    for key in unique_keys:
        where_parts.append(
            "(" + " AND ".join(f"`{col}` = %s" for col in key_cols) + ")"
        )
        params.extend(key)

    sql = (
        f"SELECT {key_select} FROM `{table}` "
        f"WHERE {' OR '.join(where_parts)}"
    )
    cur = remote_conn.cursor()
    try:
        cur.execute(sql, params)
        return {tuple(row) for row in cur.fetchall()}
    finally:
        cur.close()


def _missing_rows_for_batch(
    remote_conn,
    table: str,
    select_cols: List[str],
    key_cols: List[str],
    rows: List[tuple],
    seen_local_keys: set,
) -> List[tuple]:
    keys = [_row_key(row, select_cols, key_cols) for row in rows]
    existing = _fetch_existing_remote_keys(remote_conn, table, key_cols, keys)
    missing = []
    for row, key in zip(rows, keys):
        if key in seen_local_keys:
            continue
        seen_local_keys.add(key)
        if key not in existing:
            missing.append(row)
    return missing


def _sync_one_table(
    local_engine,
    remote_conn,
    table: str,
    spec: Dict[str, Any],
    dry_run: bool = False,
    progress_callback=None,
) -> Dict[str, Any]:
    """Sync a single table by inserting remote-missing business keys."""
    wm_col = spec["watermark_col"]
    key_cols = spec["key_cols"]
    select_cols = spec["select_cols"]
    insert_cols = spec["insert_cols"]
    col_list = ", ".join(f"`{c}`" for c in select_cols)
    placeholders = ", ".join(["%s"] * len(insert_cols))
    insert_col_list = ", ".join(f"`{c}`" for c in insert_cols)

    missing_key_cols = [col for col in key_cols if col not in select_cols]
    if missing_key_cols:
        raise ValueError(f"{table} key columns missing from select_cols: {missing_key_cols}")

    order_cols = list(dict.fromkeys([wm_col] + key_cols))
    order_list = ", ".join(f"`{col}` ASC" for col in order_cols)
    select_sql = f"SELECT {col_list} FROM `{table}` ORDER BY {order_list}"
    insert_sql = (
        f"INSERT IGNORE INTO `{table}` ({insert_col_list}) VALUES ({placeholders})"
    )

    # Read from local in batches
    total_scanned = 0
    total_missing = 0
    total_inserted = 0
    batch_num = 0
    seen_local_keys = set()
    t0 = time.time()

    with local_engine.connect() as local_conn:
        result = local_conn.execute(text(select_sql))

        batch: List[tuple] = []
        for row in result:
            batch.append(tuple(row))
            if len(batch) >= BATCH_SIZE:
                missing_rows = _missing_rows_for_batch(
                    remote_conn, table, select_cols, key_cols, batch, seen_local_keys,
                )
                total_scanned += len(batch)
                total_missing += len(missing_rows)
                if missing_rows and not dry_run:
                    remote_cur = remote_conn.cursor()
                    try:
                        remote_cur.executemany(insert_sql, missing_rows)
                        remote_conn.commit()
                        total_inserted += int(remote_cur.rowcount or 0)
                    finally:
                        remote_cur.close()
                batch_num += 1
                if progress_callback:
                    progress_callback(table, total_inserted, batch_num)
                batch = []

        # Flush remaining
        if batch:
            missing_rows = _missing_rows_for_batch(
                remote_conn, table, select_cols, key_cols, batch, seen_local_keys,
            )
            total_scanned += len(batch)
            total_missing += len(missing_rows)
            if missing_rows and not dry_run:
                remote_cur = remote_conn.cursor()
                try:
                    remote_cur.executemany(insert_sql, missing_rows)
                    remote_conn.commit()
                    total_inserted += int(remote_cur.rowcount or 0)
                finally:
                    remote_cur.close()
            batch_num += 1

    elapsed = round(time.time() - t0, 1)
    return {
        "table": table,
        "scanned_rows": total_scanned,
        "missing_rows": total_missing,
        "synced_rows": 0 if dry_run else total_inserted,
        "batches_checked": batch_num,
        "elapsed_seconds": elapsed,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sync_tables(
    tables: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run append-only key-based sync for specified tables (or all).

    Args:
        tables: list of table names to sync; None = all three.
        dry_run: if True, only report missing remote keys without writing.

    Returns:
        dict with overall status and per-table results.
    """
    tables_to_sync = tables or list(SYNC_TABLES.keys())
    for t in tables_to_sync:
        if t not in SYNC_TABLES:
            raise ValueError(
                f"Unknown table: {t}. Available: {list(SYNC_TABLES.keys())}"
            )

    local_engine = _get_local_engine()
    tunnel = _SSHTunnel()
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    try:
        tunnel.open()
        remote_conn = _get_remote_connection(tunnel.local_port)

        try:
            for table in tables_to_sync:
                spec = SYNC_TABLES[table]
                wm_col = spec["watermark_col"]

                watermark = _get_remote_watermark(remote_conn, table, wm_col)
                local_rows = _count_local_table_rows(local_engine, table)

                try:
                    result = _sync_one_table(
                        local_engine, remote_conn, table, spec, dry_run=dry_run,
                    )
                    result["watermark"] = watermark
                    result["local_rows"] = local_rows
                    if dry_run:
                        result["status"] = "dry_run"
                    elif result["missing_rows"] == 0:
                        result["status"] = "up_to_date"
                    else:
                        result["status"] = "ok"
                    results.append(result)
                except Exception as exc:
                    logger.error("Sync failed for %s: %s", table, exc)
                    errors.append({
                        "table": table,
                        "error": str(exc),
                        "watermark": watermark,
                    })

        finally:
            remote_conn.close()

    except Exception as exc:
        logger.error("SSH tunnel failed: %s", exc)
        errors.append({"table": "_tunnel", "error": str(exc)})
    finally:
        tunnel.close()
        local_engine.dispose()

    overall = "ok" if not errors else ("partial" if results else "error")
    return {
        "ok": overall == "ok",
        "status": overall,
        "results": results,
        "errors": errors,
    }
