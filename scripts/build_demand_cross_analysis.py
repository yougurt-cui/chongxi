#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按病症构建用户需求洞察表。

数据粒度：
  病症 ×（全部 / 年龄段 / 品种）

指标口径：
- 改善率、病症线索数、提及品牌数：远程 8.130.170.148 的
  protein_feature_platform.cat_disease_clues。
- 产品覆盖率：远程病症下提及的品牌，关联本地标准品牌/别名/产品/配方表计算。
- 年龄段、品种和需求信号：本地 catfood_need_comment_labels。
- 趋势：本地 catfood_choice_comments_filtered_v2 中的 source_comment_time。

注意：远程病症线索没有年龄段/品种字段，因此改善率和产品覆盖率
始终是“病症级”指标，在切片行中仅作为病症背景指标展示。
"""

import argparse
import os
import socket
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config, get_mysql_config  # noqa: E402

TARGET_TABLE = "catfood_demand_cross_analysis"
NEED_TABLE = "catfood_need_comment_labels"
BHC_TABLE = "catfood_choice_comments_filtered_v2"
REMOTE_CLUES_TABLE = "cat_disease_clues"

REMOTE_SSH_HOST = os.getenv("CAT_DISEASE_SSH_HOST", "8.130.170.148")
REMOTE_SSH_PORT = int(os.getenv("CAT_DISEASE_SSH_PORT", "22"))
REMOTE_SSH_USER = os.getenv("CAT_DISEASE_SSH_USER", "root")
REMOTE_SSH_KEY = os.getenv("CAT_DISEASE_SSH_KEY", str(Path.home() / ".ssh" / "id_rsa"))
REMOTE_DB = os.getenv("CAT_DISEASE_DB", "protein_feature_platform")

# 本地需求标签 -> 远程病症名。一个标签可对应多个远程同义词。
LOCAL_TO_REMOTE_SYMPTOMS = {
    "消化系统问题>软便/拉稀": ("软便/拉稀",),
    "消化系统问题>呕吐": ("呕吐",),
    "消化系统问题>便秘": ("便秘/干硬便",),
    "消化系统问题>便臭": ("拉屎臭",),
    "皮肤与毛发问题>黑下巴": ("黑下巴",),
    "皮肤与毛发问题>掉毛": ("掉毛",),
    "皮肤与毛发问题>瘙痒/皮肤敏感": ("瘙痒/过敏",),
    "适口与进食问题>拒食/不爱吃": ("不爱吃", "不吃"),
}
REMOTE_TO_LOCAL = {
    remote: local
    for local, remote_names in LOCAL_TO_REMOTE_SYMPTOMS.items()
    for remote in remote_names
}
REMOTE_SYMPTOM_CANONICAL = {
    "不吃": "不爱吃",
}


def quote_ident(name):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"不安全的标识符: {name}")
    return f"`{name}`"


def split_multi(value):
    return [item.strip() for item in (value or "").split(" | ") if item.strip()]


def shift_year(value, years):
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, month=2, day=28)


def local_connection():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


@contextmanager
def ssh_clues_connection():
    """开发机模式：通过 SSH 隧道连接线上病症库。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    local_port = sock.getsockname()[1]
    sock.close()
    process = subprocess.Popen(
        [
            "ssh", "-i", REMOTE_SSH_KEY, "-p", str(REMOTE_SSH_PORT),
            "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30",
            "-N", "-L", f"{local_port}:127.0.0.1:3306",
            f"{REMOTE_SSH_USER}@{REMOTE_SSH_HOST}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    conn = None
    try:
        feature_cfg = get_feature_mysql_config()
        db_user = os.getenv("CAT_DISEASE_DB_USER", str(feature_cfg.get("user") or "root"))
        db_password = os.getenv("CAT_DISEASE_DB_PASSWORD", str(feature_cfg.get("password") or ""))
        for _ in range(50):
            try:
                conn = pymysql.connect(
                    host="127.0.0.1", port=local_port, user=db_user,
                    password=db_password, database=REMOTE_DB, charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor, connect_timeout=2,
                    read_timeout=120,
                )
                break
            except pymysql.MySQLError:
                if process.poll() is not None:
                    break
                time.sleep(0.1)
        if conn is None:
            raise RuntimeError(
                "无法连接远程 cat_disease_clues，请检查 SSH key 及 "
                "CAT_DISEASE_DB_USER/CAT_DISEASE_DB_PASSWORD"
            )
        yield conn
    finally:
        if conn:
            conn.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@contextmanager
def direct_clues_connection():
    """生产 API 模式：直接连接线上特征库，不建立 SSH 隧道。"""
    cfg = dict(get_feature_mysql_config())
    cfg["database"] = REMOTE_DB
    conn = pymysql.connect(
        **cfg,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=120,
    )
    try:
        yield conn
    finally:
        conn.close()


def clues_connection(mode):
    if mode == "direct":
        return direct_clues_connection()
    return ssh_clues_connection()


def load_remote_disease_metrics(conn):
    """一次性读取远程事实，后续不再依赖本地临时病症表。"""
    stats = defaultdict(lambda: {
        "primary": "", "clue_count": 0, "improved": 0, "worsened": 0,
        "uncertain": 0, "brands": set(), "min_date": None, "max_date": None,
    })
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT primary_symptom, secondary_symptom, direct, brand, event_date "
            f"FROM {quote_ident(REMOTE_CLUES_TABLE)} "
            "WHERE secondary_symptom IS NOT NULL AND TRIM(secondary_symptom) <> ''"
        )
        for row in cur:
            raw_symptom = row["secondary_symptom"].strip()
            symptom = REMOTE_SYMPTOM_CANONICAL.get(raw_symptom, raw_symptom)
            item = stats[symptom]
            item["primary"] = (row.get("primary_symptom") or item["primary"] or "").strip()
            item["clue_count"] += 1
            direct = (row.get("direct") or "").strip()
            if direct == "改善":
                item["improved"] += 1
            elif direct == "加重":
                item["worsened"] += 1
            else:
                item["uncertain"] += 1
            brand = (row.get("brand") or "").strip()
            if brand:
                item["brands"].add(brand)
            event_date = row.get("event_date")
            if event_date:
                item["min_date"] = min(item["min_date"] or event_date, event_date)
                item["max_date"] = max(item["max_date"] or event_date, event_date)
    return stats


def load_local_product_index(conn):
    """返回品牌名/别名 -> 有效产品集，以及全部有效产品数。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(DISTINCT product_id) cnt FROM catfood_standard_formula "
            "WHERE status='active' AND is_current=1"
        )
        total_products = int(cur.fetchone()["cnt"] or 0)
        cur.execute(
            "SELECT b.brand_id,b.standard_brand_name,a.alias_name "
            "FROM catfood_standard_brand b "
            "LEFT JOIN catfood_standard_brand_alias a ON a.brand_id=b.brand_id AND a.active=1 "
            "WHERE b.active=1"
        )
        name_to_ids = defaultdict(set)
        for row in cur:
            for name in (row["standard_brand_name"], row["alias_name"]):
                if name:
                    name_to_ids[name.strip()].add(row["brand_id"])
        cur.execute(
            "SELECT p.brand_id,p.product_id FROM catfood_standard_product p "
            "INNER JOIN catfood_standard_formula f ON f.product_id=p.product_id "
            "AND f.status='active' AND f.is_current=1 WHERE p.active=1"
        )
        brand_products = defaultdict(set)
        for row in cur:
            brand_products[row["brand_id"]].add(row["product_id"])
    return name_to_ids, brand_products, total_products


def calculate_product_coverage(disease_stats, name_to_ids, brand_products, total_products):
    for item in disease_stats.values():
        products, matched_brands = set(), set()
        for brand in item["brands"]:
            brand_ids = name_to_ids.get(brand, set())
            if brand_ids:
                matched_brands.add(brand)
            for brand_id in brand_ids:
                products.update(brand_products.get(brand_id, set()))
        item["matched_brands"] = matched_brands
        item["products"] = products
        item["coverage_rate"] = len(products) / total_products if total_products else None


def load_local_segments(conn):
    """按本地需求标签组装病症的全部/年龄段/品种评论集。"""
    segments = defaultdict(set)
    text_by_key = {}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT source_comment_id,source_platform,comment_text,need_life_stage,"
            f"need_breed,health_secondary FROM {quote_ident(NEED_TABLE)} "
            "WHERE health_secondary IS NOT NULL AND TRIM(health_secondary)<>''"
        )
        for row in cur:
            key = (row["source_platform"], row["source_comment_id"])
            text_by_key[key] = (row.get("comment_text") or "").strip()
            for local_symptom in split_multi(row["health_secondary"]):
                for remote_symptom in LOCAL_TO_REMOTE_SYMPTOMS.get(local_symptom, ()):
                    segments[(remote_symptom, "all", "全部")].add(key)
                    for stage in split_multi(row.get("need_life_stage")):
                        segments[(remote_symptom, "life_stage", stage)].add(key)
                    for breed in split_multi(row.get("need_breed")):
                        segments[(remote_symptom, "breed", breed)].add(key)
    return segments, text_by_key


def load_comment_dates(conn, texts):
    result = defaultdict(set)
    values = list({text for text in texts if text})
    with conn.cursor() as cur:
        for offset in range(0, len(values), 5000):
            batch = values[offset:offset + 5000]
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(
                f"SELECT comment_text,STR_TO_DATE(NULLIF(SUBSTRING(TRIM(source_comment_time),1,10),''),'%%Y-%%m-%%d') AS event_date "
                f"FROM {quote_ident(BHC_TABLE)} "
                f"WHERE comment_text IN ({placeholders}) "
                f"AND source_comment_time IS NOT NULL AND TRIM(source_comment_time) <> ''",
                batch,
            )
            for row in cur:
                if row.get("event_date"):
                    result[(row["comment_text"] or "").strip()].add(row["event_date"])
    return result


def calculate_period_counts(comment_keys, text_by_key, dates_by_text, analysis_date):
    recent_start = shift_year(analysis_date, -1) + timedelta(days=1)
    previous_start = shift_year(analysis_date, -2) + timedelta(days=1)
    recent, previous = set(), set()
    for key in comment_keys:
        for event_date in dates_by_text.get(text_by_key.get(key, ""), set()):
            if recent_start <= event_date <= analysis_date:
                recent.add(key)
            elif previous_start <= event_date < recent_start:
                previous.add(key)
    recent_count, previous_count = len(recent), len(previous)
    yoy = None if previous_count == 0 else (recent_count - previous_count) / previous_count * 100
    return recent_count, previous_count, yoy


def ensure_target_table(conn):
    columns = {
        "category_type": "VARCHAR(16) NOT NULL",
        "category_value": "VARCHAR(64) NOT NULL",
        "health_primary": "VARCHAR(64) NOT NULL",
        "health_secondary": "VARCHAR(128) NOT NULL",
        "cross_demand": "VARCHAR(255) NOT NULL",
        "need_signal_count": "INT NOT NULL DEFAULT 0",
        "recent_12m_count": "INT NOT NULL DEFAULT 0",
        "prev_12m_count": "INT NOT NULL DEFAULT 0",
        "yoy_change_pct": "DECIMAL(10,2) NULL",
        "improvement_rate": "DECIMAL(10,4) NULL",
        "improvement_sample": "INT NOT NULL DEFAULT 0",
        "product_coverage_count": "INT NOT NULL DEFAULT 0",
        "product_coverage_rate": "DECIMAL(10,4) NULL",
        "disease_clue_count": "INT NOT NULL DEFAULT 0 COMMENT '远程病症线索数'",
        "uncertain_sample": "INT NOT NULL DEFAULT 0 COMMENT 'direct 为不确定的样本数'",
        "mentioned_brand_count": "INT NOT NULL DEFAULT 0 COMMENT '病症线索提及品牌数'",
        "matched_brand_count": "INT NOT NULL DEFAULT 0 COMMENT '成功关联本地标准品牌数'",
        "product_total_count": "INT NOT NULL DEFAULT 0 COMMENT '产品覆盖率分母'",
        "metric_scope": "VARCHAR(32) NOT NULL DEFAULT '病症级'",
        "cross_rank": "INT NULL COMMENT '年龄/品种交叉需求按近12月信号量的排名'",
        "analysis_date": "DATE NULL",
        "generated_at": "DATETIME NOT NULL",
    }
    with conn.cursor() as cur:
        definitions = ",\n".join(f"{quote_ident(k)} {v}" for k, v in columns.items())
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {quote_ident(TARGET_TABLE)} ("
            "id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,"
            f"{definitions},"
            "UNIQUE KEY uq_cross(category_type,category_value,health_secondary),"
            "KEY idx_symptom(health_secondary),KEY idx_signal(need_signal_count)"
            ") DEFAULT CHARSET=utf8mb4 COMMENT='按病症及用户切片的需求洞察表'"
        )
        cur.execute(f"SHOW COLUMNS FROM {quote_ident(TARGET_TABLE)}")
        existing = {row["Field"] for row in cur.fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                cur.execute(
                    f"ALTER TABLE {quote_ident(TARGET_TABLE)} ADD COLUMN "
                    f"{quote_ident(name)} {definition}"
                )
    conn.commit()


def build_rows(local_conn, disease_stats):
    name_to_ids, brand_products, total_products = load_local_product_index(local_conn)
    calculate_product_coverage(disease_stats, name_to_ids, brand_products, total_products)
    segments, text_by_key = load_local_segments(local_conn)
    dates_by_text = load_comment_dates(local_conn, text_by_key.values())
    matched_dates = [d for dates in dates_by_text.values() for d in dates]
    remote_dates = [item["max_date"] for item in disease_stats.values() if item["max_date"]]
    analysis_date = max(matched_dates or remote_dates or [date.today()])

    # 远程已有的所有病症都保留“全部”行，即使本地尚无切片标签。
    for symptom in disease_stats:
        segments.setdefault((symptom, "all", "全部"), set())

    rows = []
    for (symptom, category_type, category_value), comment_keys in segments.items():
        item = disease_stats.get(symptom)
        if not item:
            continue
        recent, previous, yoy = calculate_period_counts(
            comment_keys, text_by_key, dates_by_text, analysis_date
        )
        outcome_sample = item["improved"] + item["worsened"]
        improvement_rate = item["improved"] / outcome_sample if outcome_sample else None
        local_symptom = REMOTE_TO_LOCAL.get(symptom)
        health_primary = (
            local_symptom.split(">", 1)[0] if local_symptom else item["primary"]
        )
        display_symptom = (
            local_symptom.split(">", 1)[1] if local_symptom else symptom
        )
        cross_demand = display_symptom if category_type == "all" else f"{category_value}×{display_symptom}"
        rows.append({
            "category_type": category_type,
            "category_value": category_value,
            "health_primary": health_primary or "未分类",
            "health_secondary": local_symptom or symptom,
            "cross_demand": cross_demand,
            "need_signal_count": len(comment_keys),
            "recent_12m_count": recent,
            "prev_12m_count": previous,
            "yoy_change_pct": round(yoy, 2) if yoy is not None else None,
            "improvement_rate": round(improvement_rate, 4) if improvement_rate is not None else None,
            "improvement_sample": outcome_sample,
            "product_coverage_count": len(item["products"]),
            "product_coverage_rate": round(item["coverage_rate"], 4) if item["coverage_rate"] is not None else None,
            "disease_clue_count": item["clue_count"],
            "uncertain_sample": item["uncertain"],
            "mentioned_brand_count": len(item["brands"]),
            "matched_brand_count": len(item["matched_brands"]),
            "product_total_count": total_products,
            "metric_scope": "病症级",
            "cross_rank": None,
            "analysis_date": analysis_date,
            "generated_at": datetime.now(),
        })
    ranked_cross_rows = sorted(
        (row for row in rows if row["category_type"] != "all"),
        key=lambda row: (-row["recent_12m_count"], -row["need_signal_count"], row["cross_demand"]),
    )
    for rank, row in enumerate(ranked_cross_rows, 1):
        row["cross_rank"] = rank

    rows.sort(key=lambda row: (
        row["category_type"] != "all", -row["disease_clue_count"],
        -row["need_signal_count"], row["cross_demand"],
    ))
    return rows


def replace_rows(conn, rows):
    ensure_target_table(conn)
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {quote_ident(TARGET_TABLE)}")
        if rows:
            columns = list(rows[0])
            sql = (
                f"INSERT INTO {quote_ident(TARGET_TABLE)} "
                f"({','.join(quote_ident(c) for c in columns)}) VALUES "
                f"({','.join('%(' + c + ')s' for c in columns)})"
            )
            for offset in range(0, len(rows), 500):
                cur.executemany(sql, rows[offset:offset + 500])
    conn.commit()


def parse_args():
    parser = argparse.ArgumentParser(description="按病症构建用户需求洞察表")
    parser.add_argument("--dry-run", action="store_true", help="只计算和打印，不写入数据库")
    parser.add_argument(
        "--clues-connection",
        choices=("ssh", "direct"),
        default="ssh",
        help="病症线索库连接方式：开发机默认 ssh，生产 API 使用 direct",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    print(f"[1/4] 读取远程 {REMOTE_DB}.{REMOTE_CLUES_TABLE} ...")
    with clues_connection(args.clues_connection) as remote_conn:
        disease_stats = load_remote_disease_metrics(remote_conn)
    print(f"  病症数: {len(disease_stats)}, 线索数: {sum(x['clue_count'] for x in disease_stats.values())}")

    local_conn = local_connection()
    try:
        print("[2/4] 关联本地品牌、产品及用户切片 ...")
        rows = build_rows(local_conn, disease_stats)
        overall = [row for row in rows if row["category_type"] == "all"]
        print(f"[3/4] 生成 {len(overall)} 条病症总览 + {len(rows) - len(overall)} 条年龄/品种切片")
        if not args.dry_run:
            replace_rows(local_conn, rows)
            print(f"[4/4] 已写入 {TARGET_TABLE}: {len(rows)} 条")
        else:
            print("[4/4] dry-run，未写入数据库")
        print("\n病症总览：")
        for row in overall:
            rate = "-" if row["improvement_rate"] is None else f"{row['improvement_rate']:.1%}"
            coverage = f"{row['product_coverage_count']}/{row['product_total_count']}"
            print(
                f"  {row['cross_demand']}: 线索={row['disease_clue_count']}, "
                f"改善率={rate}(n={row['improvement_sample']}), 产品覆盖={coverage}, "
                f"品牌匹配={row['matched_brand_count']}/{row['mentioned_brand_count']}, "
                f"需求评论={row['need_signal_count']}"
            )
    except Exception:
        local_conn.rollback()
        raise
    finally:
        local_conn.close()


if __name__ == "__main__":
    main()
