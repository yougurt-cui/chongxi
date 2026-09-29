"""MySQL storage for the shared comment layer and router."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pymysql

from app_config import get_mysql_config
from comment_pipeline.common.cleaner import CleanedComment
from comment_pipeline.router import RouteResult
from scripts.filter_catfood_choice_comments import BRAND_TERMS


def connect(cursorclass=pymysql.cursors.DictCursor):
    return pymysql.connect(**get_mysql_config(), autocommit=False, cursorclass=cursorclass)


def init_tables(conn) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS comment_clean_base (
              comment_id BIGINT NOT NULL AUTO_INCREMENT,
              platform VARCHAR(32) NOT NULL,
              source_table VARCHAR(64) NOT NULL,
              source_id VARCHAR(255) NOT NULL,
              source_row_id BIGINT NULL,
              raw_text LONGTEXT NOT NULL,
              clean_text LONGTEXT NOT NULL,
              text_hash CHAR(64) NOT NULL,
              brand VARCHAR(255) NOT NULL DEFAULT '',
              product_name VARCHAR(500) NOT NULL DEFAULT '',
              product_category VARCHAR(128) NOT NULL DEFAULT '',
              source_title TEXT NULL,
              source_content MEDIUMTEXT NULL,
              source_keyword VARCHAR(255) NULL,
              source_created_at VARCHAR(64) NULL,
              source_like_count INT NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (comment_id),
              UNIQUE KEY uq_clean_source (platform,source_table,source_id),
              UNIQUE KEY uq_clean_text (platform,text_hash),
              KEY idx_clean_brand (brand),
              KEY idx_clean_product_category (product_category)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS comment_router_result (
              id BIGINT NOT NULL AUTO_INCREMENT,
              comment_id BIGINT NOT NULL,
              choice_match TINYINT(1) NOT NULL DEFAULT 0,
              product_preference_match TINYINT(1) NOT NULL DEFAULT 0,
              route_labels JSON NULL,
              router_source VARCHAR(32) NOT NULL,
              created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
              PRIMARY KEY (id),
              UNIQUE KEY uq_router_comment (comment_id),
              KEY idx_router_choice (choice_match),
              KEY idx_router_preference (product_preference_match)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute("SHOW COLUMNS FROM comment_router_result")
        router_columns = {row["Field"] for row in cursor.fetchall()}
        if "route_labels" not in router_columns:
            cursor.execute("ALTER TABLE comment_router_result ADD COLUMN route_labels JSON NULL AFTER router_source")
    conn.commit()


def load_brand_aliases(conn) -> dict[str, str]:
    aliases: dict[str, str] = {}
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT b.standard_brand_name,a.alias_name
                FROM catfood_standard_brand b
                LEFT JOIN catfood_standard_brand_alias a
                  ON a.brand_id=b.brand_id AND a.active=1
                WHERE b.active=1
                """
            )
            for row in cursor.fetchall():
                standard = str(row.get("standard_brand_name") or "").strip()
                alias = str(row.get("alias_name") or "").strip()
                if standard:
                    aliases[standard] = standard
                    if alias:
                        aliases[alias] = standard
    except pymysql.MySQLError:
        # The choice script keeps its own fallback brand dictionary. The common
        # layer stays usable even when brand master tables have not been created.
        aliases = {}
    if not aliases:
        aliases = {name: name for name in BRAND_TERMS.split("|") if name}
    return aliases


def load_product_aliases(conn) -> dict[str, tuple[str, str]]:
    aliases: dict[str, tuple[str, str]] = {}
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.standard_product_name,p.display_name,p.product_type,a.alias_name
                FROM catfood_standard_product p
                LEFT JOIN catfood_standard_product_alias a
                  ON a.product_id=p.product_id AND a.active=1
                WHERE p.active=1
                """
            )
            for row in cursor.fetchall():
                standard = str(row.get("standard_product_name") or "").strip()
                category = str(row.get("product_type") or "猫粮").strip()
                for value in (standard, row.get("display_name"), row.get("alias_name")):
                    alias = str(value or "").strip()
                    if alias and standard:
                        aliases[alias] = (standard, category)
    except pymysql.MySQLError:
        return {}
    return aliases


def upsert_clean_comment(conn, comment: CleanedComment) -> tuple[int, bool]:
    values = asdict(comment)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT comment_id FROM comment_clean_base
            WHERE platform=%s AND (text_hash=%s OR (source_table=%s AND source_id=%s))
            ORDER BY comment_id LIMIT 1
            """,
            (comment.platform, comment.text_hash, comment.source_table, comment.source_id),
        )
        existing = cursor.fetchone()
        if existing:
            return int(existing["comment_id"]), False
        cursor.execute(
            """
            INSERT INTO comment_clean_base(
              platform,source_table,source_id,source_row_id,raw_text,clean_text,text_hash,
              brand,product_name,product_category,source_title,source_content,
              source_keyword,source_created_at,source_like_count
            ) VALUES(
              %(platform)s,%(source_table)s,%(source_id)s,%(source_row_id)s,%(raw_text)s,
              %(clean_text)s,%(text_hash)s,%(brand)s,%(product_name)s,%(product_category)s,
              %(source_title)s,%(source_content)s,%(source_keyword)s,%(source_created_at)s,
              %(source_like_count)s
            )
            """,
            values,
        )
        comment_id = int(cursor.lastrowid)
    conn.commit()
    return comment_id, True


def get_route(conn, comment_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM comment_router_result WHERE comment_id=%s", (comment_id,))
        return cursor.fetchone()


def upsert_route(conn, comment_id: int, route: RouteResult) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO comment_router_result(
              comment_id,choice_match,product_preference_match,router_source,route_labels
            ) VALUES(%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              choice_match=VALUES(choice_match),
              product_preference_match=VALUES(product_preference_match),
              route_labels=VALUES(route_labels),
              router_source=VALUES(router_source),updated_at=NOW()
            """,
            (
                comment_id, int(route.choice), int(route.product_preference), route.router_source,
                json.dumps([
                    label for label, matched in (
                        ("choice", route.choice),
                        ("product_preference", route.product_preference),
                    ) if matched
                ], ensure_ascii=False),
            ),
        )
    conn.commit()
