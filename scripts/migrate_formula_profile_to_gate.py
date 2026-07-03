#!/usr/bin/env python3
"""Convert formula profile into a domain gate and archive/clear derived tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config, get_mysql_config  # noqa: E402

PROFILE_TABLE = "catfood_formula_feature_profile"
ITEM_TABLE = "catfood_formula_ingredient_item"
DERIVED_TABLES = (
    "protein_source_aggregate",
    "protein_business_cluster_product_details_scored",
    "catfood_fiber_feature_json",
    "catfood_fiber_feature_score",
    "catfood_fat_material_features",
    "catfood_fat_material_features_scored",
    "catfood_protein_fat_fiber_score_wide",
    "sku_feature_input",
    "sku_risk_score_result",
)
DOMAINS = ("protein", "fat", "fiber", "starch")


def _affected_domains(raw_name: str, item: dict[str, Any]) -> list[str]:
    raw = str(raw_name or "")
    domains = []
    if item.get("is_protein") or any(marker in raw for marker in ("肉", "鱼", "蛋白", "肝", "心", "蛋")):
        domains.append("protein")
    if any(marker in raw for marker in ("油", "脂", "磷虾")):
        domains.append("fat")
    if any(marker in raw for marker in ("纤维", "菊粉", "车前子", "甜菜粕", "果寡糖", "寡糖")):
        domains.append("fiber")
    if any(marker in raw for marker in ("淀粉", "薯", "米", "麦", "豆")) and "蛋白" not in raw:
        domains.append("starch")
    return sorted(set(domains))


def _profile_ddl(table: str) -> str:
    return f"""
        CREATE TABLE `{table}` (
          formula_id BIGINT UNSIGNED NOT NULL,
          ingredient_fingerprint CHAR(64) NOT NULL,
          ingredient_count INT NOT NULL DEFAULT 0,
          effective_ingredient_count INT NOT NULL DEFAULT 0,
          standardized_ingredient_count INT NOT NULL DEFAULT 0,
          ignored_ingredient_count INT NOT NULL DEFAULT 0,
          unmatched_ingredient_count INT NOT NULL DEFAULT 0,
          blocking_ingredient_count INT NOT NULL DEFAULT 0,
          warning_ingredient_count INT NOT NULL DEFAULT 0,
          standardization_coverage DECIMAL(8,5) NOT NULL DEFAULT 0,
          overall_status VARCHAR(32) NOT NULL,
          protein_status VARCHAR(32) NOT NULL,
          fat_status VARCHAR(32) NOT NULL,
          fiber_status VARCHAR(32) NOT NULL,
          starch_status VARCHAR(32) NOT NULL,
          domain_gate_json JSON NOT NULL,
          dirty_domains_json JSON NOT NULL,
          quality_metrics_json JSON NOT NULL,
          input_hash CHAR(64) NOT NULL,
          profile_version VARCHAR(32) NOT NULL DEFAULT 'gate-v1',
          rule_versions_json JSON NULL,
          created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (formula_id),
          KEY idx_overall_status (overall_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """


def _ensure_item_review_schema(cursor) -> None:
    cursor.execute(f"SHOW COLUMNS FROM `{ITEM_TABLE}`")
    columns = {row["Field"] for row in cursor.fetchall()}
    additions = {
        "match_status": "VARCHAR(32) NULL",
        "review_status": "VARCHAR(32) NULL",
        "issue_severity": "VARCHAR(32) NULL",
        "affected_domains_json": "JSON NULL",
        "is_ignored": "TINYINT NOT NULL DEFAULT 0",
        "review_reason": "VARCHAR(255) NULL",
        "reviewer": "VARCHAR(128) NULL",
        "reviewed_at": "DATETIME NULL",
    }
    for name, ddl in additions.items():
        if name not in columns:
            cursor.execute(f"ALTER TABLE `{ITEM_TABLE}` ADD COLUMN `{name}` {ddl}")


def _build_profiles(cursor, *, update_items: bool) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT f.formula_id, f.ingredient_fingerprint, i.*
        FROM catfood_standard_formula f
        LEFT JOIN catfood_formula_ingredient_item i ON i.formula_id=f.formula_id
        WHERE f.status='active' AND f.is_current=1
        ORDER BY f.formula_id, i.position
        """
    )
    grouped: dict[int, dict[str, Any]] = {}
    for row in cursor.fetchall():
        formula_id = int(row["formula_id"])
        grouped.setdefault(
            formula_id,
            {"fingerprint": str(row["ingredient_fingerprint"]), "items": []},
        )
        if row.get("item_id") is not None:
            grouped[formula_id]["items"].append(dict(row))

    profiles = []
    for formula_id, payload in grouped.items():
        items = payload["items"]
        blocking_domains: set[str] = set()
        warning_count = 0
        blocking_count = 0
        standardized_count = 0
        ignored_count = 0
        item_updates = []
        for item in items:
            ignored = bool(item.get("is_ignored"))
            matched = bool(item.get("standard_ingredient_id"))
            domains = _affected_domains(item.get("raw_name"), item)
            if ignored:
                match_status, review_status, severity = "ignored", "approved", "warning"
                ignored_count += 1
            elif matched:
                match_status, review_status, severity = "matched", "auto_approved", None
                standardized_count += 1
            else:
                match_status, review_status = "unmatched", "pending"
                severity = "blocking" if domains else "warning"
                if severity == "blocking":
                    blocking_domains.update(domains)
                    blocking_count += 1
                else:
                    warning_count += 1
            item_updates.append((
                match_status, review_status, severity,
                json.dumps(domains, ensure_ascii=False), int(ignored), item["item_id"],
            ))
        if update_items and item_updates:
            cursor.executemany(
                f"""
                UPDATE `{ITEM_TABLE}` SET match_status=%s, review_status=%s,
                  issue_severity=%s, affected_domains_json=%s, is_ignored=%s
                WHERE item_id=%s
                """,
                item_updates,
            )

        effective_count = len(items) - ignored_count
        unmatched_count = max(effective_count - standardized_count, 0)
        coverage = standardized_count / effective_count if effective_count else 0.0
        gates = {}
        dirty_domains = []
        for domain in DOMAINS:
            allowed = bool(items) and domain not in blocking_domains
            status = "pending_rebuild" if allowed else "need_review"
            gates[domain] = {
                "allowed": allowed,
                "status": status,
                "reason": None if allowed else "存在未完成标准化的相关原料",
            }
            if allowed:
                dirty_domains.append(domain)
        allowed_count = sum(bool(gates[domain]["allowed"]) for domain in DOMAINS)
        overall = "ready_for_rebuild" if allowed_count == len(DOMAINS) else "partially_ready" if allowed_count else "need_review"
        quality = {
            "ingredient_count": len(items),
            "effective_ingredient_count": effective_count,
            "standardized_count": standardized_count,
            "ignored_count": ignored_count,
            "unmatched_count": unmatched_count,
            "blocking_count": blocking_count,
            "warning_count": warning_count,
            "coverage": round(coverage, 5),
        }
        input_hash = hashlib.sha256(
            json.dumps(
                {
                    "fingerprint": payload["fingerprint"],
                    "items": [
                        [item.get("position"), item.get("standard_ingredient_id"), item.get("is_ignored")]
                        for item in items
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        profiles.append({
            "formula_id": formula_id,
            "ingredient_fingerprint": payload["fingerprint"],
            "ingredient_count": len(items),
            "effective_ingredient_count": effective_count,
            "standardized_ingredient_count": standardized_count,
            "ignored_ingredient_count": ignored_count,
            "unmatched_ingredient_count": unmatched_count,
            "blocking_ingredient_count": blocking_count,
            "warning_ingredient_count": warning_count,
            "standardization_coverage": round(coverage, 5),
            "overall_status": overall,
            **{f"{domain}_status": gates[domain]["status"] for domain in DOMAINS},
            "domain_gate_json": json.dumps(gates, ensure_ascii=False),
            "dirty_domains_json": json.dumps(dirty_domains, ensure_ascii=False),
            "quality_metrics_json": json.dumps(quality, ensure_ascii=False),
            "input_hash": input_hash,
            "profile_version": "gate-v1",
            "rule_versions_json": json.dumps({"gate": "gate-v1"}, ensure_ascii=False),
        })
    return profiles


def migrate(*, apply: bool) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_cfg = get_mysql_config()
    feature_cfg = get_feature_mysql_config()
    profile_backup = f"{PROFILE_TABLE}_bak_gate_{timestamp}"
    downstream_backups = {
        table: f"{table[:40]}_bg_{timestamp}"
        for table in DERIVED_TABLES
    }
    summary: dict[str, Any] = {
        "ok": True,
        "applied": apply,
        "profile_backup": profile_backup if apply else None,
        "downstream_backups": downstream_backups if apply else {},
        "downstream_rows": {},
    }

    with pymysql.connect(**feature_cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            for table in DERIVED_TABLES:
                cursor.execute("SHOW TABLES LIKE %s", (table,))
                if not cursor.fetchone():
                    continue
                cursor.execute(f"SELECT COUNT(*) n FROM `{table}`")
                summary["downstream_rows"][table] = int(cursor.fetchone()["n"])
                if apply:
                    backup = downstream_backups[table]
                    cursor.execute(f"CREATE TABLE `{backup}` LIKE `{table}`")
                    cursor.execute(f"INSERT INTO `{backup}` SELECT * FROM `{table}`")
                    cursor.execute(f"DELETE FROM `{table}`")
        if apply:
            conn.commit()
        else:
            conn.rollback()

    with pymysql.connect(**csv_cfg, cursorclass=pymysql.cursors.DictCursor, autocommit=False) as conn:
        with conn.cursor() as cursor:
            if apply:
                cursor.execute(f"CREATE TABLE `{profile_backup}` LIKE `{PROFILE_TABLE}`")
                cursor.execute(f"INSERT INTO `{profile_backup}` SELECT * FROM `{PROFILE_TABLE}`")
                _ensure_item_review_schema(cursor)
            profiles = _build_profiles(cursor, update_items=apply)
            summary["profile_rows"] = len(profiles)
            summary["profile_statuses"] = {
                status: sum(row["overall_status"] == status for row in profiles)
                for status in ("ready_for_rebuild", "partially_ready", "need_review")
            }
            if apply:
                stage = f"{PROFILE_TABLE}_stage_gate"
                cursor.execute(f"DROP TABLE IF EXISTS `{stage}`")
                cursor.execute(_profile_ddl(stage))
                columns = list(profiles[0]) if profiles else []
                placeholders = ",".join(["%s"] * len(columns))
                cursor.executemany(
                    f"INSERT INTO `{stage}` ({','.join(f'`{name}`' for name in columns)}) VALUES({placeholders})",
                    [tuple(row[name] for name in columns) for row in profiles],
                )
                cursor.execute(f"RENAME TABLE `{PROFILE_TABLE}` TO `{PROFILE_TABLE}_legacy_swap`, `{stage}` TO `{PROFILE_TABLE}`")
                cursor.execute(f"DROP TABLE `{PROFILE_TABLE}_legacy_swap`")
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(apply=bool(args.apply)), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
