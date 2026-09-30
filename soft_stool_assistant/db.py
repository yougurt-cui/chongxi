"""Read-only database helpers using the application's shared configuration."""

from __future__ import annotations

from typing import Any

import pymysql

from app_config import get_mysql_config


def _connect():
    return pymysql.connect(
        **get_mysql_config(),
        autocommit=True,
        connect_timeout=8,
        read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def query_all(sql: str, params: Any = None) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())


def query_one(sql: str, params: Any = None) -> dict[str, Any] | None:
    rows = query_all(sql, params)
    return rows[0] if rows else None


def query_scalar(sql: str, params: Any = None, default: Any = None) -> Any:
    row = query_one(sql, params)
    return next(iter(row.values())) if row else default
