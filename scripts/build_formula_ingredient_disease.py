#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建“病症 -> 代表产品 -> 当前配方 -> 标准原料”关联与排名。

远程 cat_disease_clues 只能识别品牌，不能识别具体 product_id，因此：
- 代表产品 = 病症相关标准品牌旗下、本地已建立当前有效配方的产品；
- 代表原料 = 这些产品当前配方中高频出现的标准原料；
- 改善率始终是“病症 × 品牌”证据，不是产品或原料因果效果。

输出（本地 csv_labeling）：
- catfood_formula_ingredient_disease：病症×产品×配方×标准原料明细桥表
- catfood_disease_representative_product：每个病症的代表产品
- catfood_disease_representative_ingredient：每个病症的代表原料
"""

import argparse
import math
import os
import re
import socket
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_feature_mysql_config, get_mysql_config  # noqa: E402

DETAIL_TABLE = "catfood_formula_ingredient_disease"
PRODUCT_TABLE = "catfood_disease_representative_product"
INGREDIENT_TABLE = "catfood_disease_representative_ingredient"

REMOTE_SSH_HOST = os.getenv("CAT_DISEASE_SSH_HOST", "8.130.170.148")
REMOTE_SSH_PORT = int(os.getenv("CAT_DISEASE_SSH_PORT", "22"))
REMOTE_SSH_USER = os.getenv("CAT_DISEASE_SSH_USER", "root")
REMOTE_SSH_KEY = os.getenv("CAT_DISEASE_SSH_KEY", str(Path.home() / ".ssh" / "id_rsa"))
REMOTE_DB = os.getenv("CAT_DISEASE_DB", "protein_feature_platform")
SYMPTOM_CANONICAL = {"不吃": "不爱吃"}


def quote_ident(name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"不安全的标识符: {name}")
    return f"`{name}`"


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
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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


def normalize_brand(value):
    return re.sub(r"[\s\-_·•・·]+", "", str(value or "")).casefold()


def split_brands(value):
    """仅拆明确的品牌并列分隔符，避免破坏品牌本身名称。"""
    return [part.strip() for part in re.split(r"[,，;；|]+", str(value or "")) if part.strip()]


def load_brand_master(conn):
    """返回标准化名称 -> brand_id，以及 brand_id -> 标准名。"""
    name_to_id, id_to_name = {}, {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT b.brand_id,b.standard_brand_name,a.alias_name "
            "FROM catfood_standard_brand b LEFT JOIN catfood_standard_brand_alias a "
            "ON a.brand_id=b.brand_id AND a.active=1 WHERE b.active=1"
        )
        for row in cur:
            brand_id = row["brand_id"]
            id_to_name[brand_id] = row["standard_brand_name"]
            for name in (row["standard_brand_name"], row["alias_name"]):
                key = normalize_brand(name)
                if key:
                    name_to_id[key] = brand_id
    return name_to_id, id_to_name


def load_disease_brand_stats(remote_conn, name_to_id, id_to_name):
    """将远程线索先拆品牌、再标准化，按病症×标准品牌聚合。"""
    stats = defaultdict(lambda: {
        "clue_ids": set(), "improved": 0, "worsened": 0, "uncertain": 0,
        "raw_brands": set(), "primary": "",
    })
    unmatched = defaultdict(int)
    with remote_conn.cursor() as cur:
        cur.execute(
            "SELECT id,brand,primary_symptom,secondary_symptom,direct "
            "FROM cat_disease_clues WHERE brand IS NOT NULL AND TRIM(brand)<>'' "
            "AND secondary_symptom IS NOT NULL AND TRIM(secondary_symptom)<>''"
        )
        for row in cur:
            symptom_raw = row["secondary_symptom"].strip()
            symptom = SYMPTOM_CANONICAL.get(symptom_raw, symptom_raw)
            for raw_brand in split_brands(row["brand"]):
                brand_id = name_to_id.get(normalize_brand(raw_brand))
                if brand_id is None:
                    unmatched[raw_brand] += 1
                    continue
                item = stats[(symptom, brand_id)]
                item["primary"] = (row.get("primary_symptom") or item["primary"] or "").strip()
                item["raw_brands"].add(raw_brand)
                # 同一线索中同一标准品牌的多个别名只计一次。
                if row["id"] in item["clue_ids"]:
                    continue
                item["clue_ids"].add(row["id"])
                direct = (row.get("direct") or "").strip()
                if direct == "改善":
                    item["improved"] += 1
                elif direct == "加重":
                    item["worsened"] += 1
                else:
                    item["uncertain"] += 1
    for (symptom, brand_id), item in stats.items():
        item["symptom"] = symptom
        item["brand_id"] = brand_id
        item["brand_name"] = id_to_name[brand_id]
        item["clue_count"] = len(item.pop("clue_ids"))
        sample = item["improved"] + item["worsened"]
        item["improvement_sample"] = sample
        item["improvement_rate"] = item["improved"] / sample if sample else None
        item["wilson_lower"] = wilson_lower(item["improved"], sample)
    return stats, unmatched


def wilson_lower(successes, sample, z=1.96):
    if not sample:
        return 0.0
    p = successes / sample
    denominator = 1 + z * z / sample
    center = p + z * z / (2 * sample)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * sample)) / sample)
    return (center - margin) / denominator


def load_formula_ingredient_chain(conn):
    """以本地标准产品和当前有效配方为主表，读取一行一标准原料。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.brand_id,b.standard_brand_name,p.product_id,
                   COALESCE(NULLIF(p.display_name,''),p.standard_product_name) product_name,
                   p.life_stage,p.product_type,f.formula_id,f.formula_version,
                   i.item_id,i.position,i.raw_name,i.standard_ingredient_id,
                   COALESCE(s.standard_name,i.standard_name) standard_name,
                   COALESCE(s.ingredient_family,i.ingredient_family) ingredient_family,
                   COALESCE(s.source_type,i.source_type) source_type,
                   COALESCE(s.animal_source,i.animal_source) animal_source,
                   COALESCE(s.primary_nutrition_role,i.primary_nutrition_role) primary_nutrition_role,
                   i.confidence,i.match_status,i.review_status
            FROM catfood_standard_brand b
            JOIN catfood_standard_product p ON p.brand_id=b.brand_id AND p.active=1
            JOIN catfood_standard_formula f ON f.product_id=p.product_id
                 AND f.status='active' AND f.is_current=1
            JOIN catfood_formula_ingredient_item i ON i.formula_id=f.formula_id
                 AND COALESCE(i.is_ignored,0)=0 AND i.standard_ingredient_id IS NOT NULL
                 AND i.match_status='matched'
            JOIN catfood_standard_ingredient s
                 ON s.standard_ingredient_id=i.standard_ingredient_id AND s.active=1
            WHERE b.active=1
            ORDER BY b.brand_id,p.product_id,i.position
            """
        )
        rows = list(cur.fetchall())
    by_brand = defaultdict(list)
    for row in rows:
        by_brand[row["brand_id"]].append(row)
    return by_brand


def representative_product_score(stats):
    """品牌代理证据分：线索体量与 Wilson 改善率下界的组合。"""
    return math.log1p(stats["clue_count"]) * (0.5 + 0.5 * stats["wilson_lower"])


def build_outputs(disease_stats, chain_by_brand):
    detail_rows, products = [], {}
    for (symptom, brand_id), stats in disease_stats.items():
        chain = chain_by_brand.get(brand_id, [])
        if not chain:
            continue
        by_product = defaultdict(list)
        for item in chain:
            by_product[item["product_id"]].append(item)
        for product_id, items in by_product.items():
            first = items[0]
            product_key = (symptom, product_id)
            products[product_key] = {
                "primary_symptom": stats["primary"] or "未分类",
                "secondary_symptom": symptom,
                "brand_id": brand_id,
                "brand_name": stats["brand_name"],
                "product_id": product_id,
                "product_name": first["product_name"],
                "formula_id": first["formula_id"],
                "formula_version": first["formula_version"],
                "life_stage": first["life_stage"],
                "product_type": first["product_type"],
                "disease_clue_count": stats["clue_count"],
                "improvement_count": stats["improved"],
                "worsening_count": stats["worsened"],
                "uncertain_count": stats["uncertain"],
                "improvement_rate": stats["improvement_rate"],
                "improvement_sample": stats["improvement_sample"],
                "wilson_lower_bound": stats["wilson_lower"],
                "representative_score": representative_product_score(stats),
                "evidence_scope": "disease_brand_proxy",
            }
            for item in items:
                detail_rows.append({
                    **products[product_key],
                    "item_id": item["item_id"],
                    "position": item["position"],
                    "raw_name": item["raw_name"],
                    "standard_ingredient_id": item["standard_ingredient_id"],
                    "standard_name": item["standard_name"],
                    "ingredient_family": item["ingredient_family"],
                    "source_type": item["source_type"],
                    "animal_source": item["animal_source"],
                    "primary_nutrition_role": item["primary_nutrition_role"],
                    "match_confidence": item["confidence"],
                })

    product_rows = list(products.values())
    by_symptom_products = defaultdict(list)
    for row in product_rows:
        by_symptom_products[row["secondary_symptom"]].append(row)
    for rows in by_symptom_products.values():
        rows.sort(key=lambda x: (-x["representative_score"], -x["disease_clue_count"], x["product_name"]))
        for rank, row in enumerate(rows, 1):
            row["product_rank"] = rank

    # 一个品牌对某原料只计一次病症证据，避免该品牌产品越多权重越大。
    ingredient_groups = defaultdict(lambda: {
        "brands": set(), "products": set(), "formulas": set(), "positions": [],
        "brand_stats": {}, "primary": "", "standard_name": "", "family": "",
        "source_type": "", "animal_source": "", "role": "",
    })
    for row in detail_rows:
        key = (row["secondary_symptom"], row["standard_ingredient_id"])
        group = ingredient_groups[key]
        group["primary"] = row["primary_symptom"]
        group["standard_name"] = row["standard_name"]
        group["family"] = row["ingredient_family"]
        group["source_type"] = row["source_type"]
        group["animal_source"] = row["animal_source"]
        group["role"] = row["primary_nutrition_role"]
        group["brands"].add(row["brand_id"])
        group["products"].add(row["product_id"])
        group["formulas"].add(row["formula_id"])
        group["positions"].append(row["position"])
        group["brand_stats"][row["brand_id"]] = {
            "clues": row["disease_clue_count"], "improved": row["improvement_count"],
            "worsened": row["worsening_count"], "uncertain": row["uncertain_count"],
        }

    ingredient_rows = []
    for (symptom, ingredient_id), group in ingredient_groups.items():
        supporting_clues = sum(x["clues"] for x in group["brand_stats"].values())
        improved = sum(x["improved"] for x in group["brand_stats"].values())
        worsened = sum(x["worsened"] for x in group["brand_stats"].values())
        uncertain = sum(x["uncertain"] for x in group["brand_stats"].values())
        sample = improved + worsened
        avg_position = sum(group["positions"]) / len(group["positions"])
        # 代表性优先看跨品牌覆盖，其次看跨产品覆盖和病症线索支持。
        score = len(group["brands"]) * 10 + len(group["products"]) + math.log1p(supporting_clues)
        ingredient_rows.append({
            "primary_symptom": group["primary"] or "未分类",
            "secondary_symptom": symptom,
            "standard_ingredient_id": ingredient_id,
            "standard_name": group["standard_name"],
            "ingredient_family": group["family"],
            "source_type": group["source_type"],
            "animal_source": group["animal_source"],
            "primary_nutrition_role": group["role"],
            "supporting_brand_count": len(group["brands"]),
            "supporting_product_count": len(group["products"]),
            "supporting_formula_count": len(group["formulas"]),
            "supporting_clue_count": supporting_clues,
            "improvement_count": improved,
            "worsening_count": worsened,
            "uncertain_count": uncertain,
            "improvement_rate": improved / sample if sample else None,
            "improvement_sample": sample,
            "average_position": avg_position,
            "representative_score": score,
            "evidence_scope": "disease_brand_product_cooccurrence",
        })
    by_symptom_ingredients = defaultdict(list)
    for row in ingredient_rows:
        by_symptom_ingredients[row["secondary_symptom"]].append(row)
    for rows in by_symptom_ingredients.values():
        rows.sort(key=lambda x: (-x["representative_score"], x["average_position"], x["standard_name"]))
        for rank, row in enumerate(rows, 1):
            row["ingredient_rank"] = rank

    product_rank = {(r["secondary_symptom"], r["product_id"]): r["product_rank"] for r in product_rows}
    ingredient_rank = {(r["secondary_symptom"], r["standard_ingredient_id"]): r["ingredient_rank"] for r in ingredient_rows}
    for row in detail_rows:
        row["product_rank"] = product_rank[(row["secondary_symptom"], row["product_id"])]
        row["ingredient_rank"] = ingredient_rank[(row["secondary_symptom"], row["standard_ingredient_id"])]
    return detail_rows, product_rows, ingredient_rows


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {quote_ident(DETAIL_TABLE + '_next')}")
        cur.execute(f"""
            CREATE TABLE {quote_ident(DETAIL_TABLE + '_next')} (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              primary_symptom VARCHAR(64) NOT NULL, secondary_symptom VARCHAR(64) NOT NULL,
              brand_id BIGINT NOT NULL, brand_name VARCHAR(255) NOT NULL,
              product_id BIGINT NOT NULL, product_name VARCHAR(512) NOT NULL,
              formula_id BIGINT NOT NULL, formula_version INT NOT NULL,
              life_stage VARCHAR(64) NULL, product_type VARCHAR(64) NULL,
              item_id BIGINT NOT NULL, position INT NOT NULL, raw_name VARCHAR(255) NULL,
              standard_ingredient_id VARCHAR(64) NOT NULL, standard_name VARCHAR(255) NOT NULL,
              ingredient_family VARCHAR(128) NULL, source_type VARCHAR(64) NULL,
              animal_source VARCHAR(128) NULL, primary_nutrition_role VARCHAR(128) NULL,
              match_confidence DECIMAL(6,5) NULL,
              disease_clue_count INT NOT NULL, improvement_count INT NOT NULL,
              worsening_count INT NOT NULL, uncertain_count INT NOT NULL,
              improvement_rate DECIMAL(10,4) NULL, improvement_sample INT NOT NULL,
              wilson_lower_bound DECIMAL(10,4) NOT NULL, representative_score DECIMAL(12,4) NOT NULL,
              product_rank INT NOT NULL, ingredient_rank INT NOT NULL,
              evidence_scope VARCHAR(64) NOT NULL, generated_at DATETIME NOT NULL,
              UNIQUE KEY uq_detail(secondary_symptom,product_id,formula_id,item_id),
              KEY idx_disease_product(secondary_symptom,product_rank),
              KEY idx_disease_ingredient(secondary_symptom,ingredient_rank)
            ) DEFAULT CHARSET=utf8mb4
        """)
        cur.execute(f"DROP TABLE IF EXISTS {quote_ident(PRODUCT_TABLE + '_next')}")
        cur.execute(f"""
            CREATE TABLE {quote_ident(PRODUCT_TABLE + '_next')} (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              primary_symptom VARCHAR(64) NOT NULL, secondary_symptom VARCHAR(64) NOT NULL,
              product_rank INT NOT NULL, brand_id BIGINT NOT NULL, brand_name VARCHAR(255) NOT NULL,
              product_id BIGINT NOT NULL, product_name VARCHAR(512) NOT NULL,
              formula_id BIGINT NOT NULL, formula_version INT NOT NULL,
              life_stage VARCHAR(64) NULL, product_type VARCHAR(64) NULL,
              disease_clue_count INT NOT NULL, improvement_count INT NOT NULL,
              worsening_count INT NOT NULL, uncertain_count INT NOT NULL,
              improvement_rate DECIMAL(10,4) NULL, improvement_sample INT NOT NULL,
              wilson_lower_bound DECIMAL(10,4) NOT NULL, representative_score DECIMAL(12,4) NOT NULL,
              evidence_scope VARCHAR(64) NOT NULL, generated_at DATETIME NOT NULL,
              UNIQUE KEY uq_product(secondary_symptom,product_id),
              KEY idx_rank(secondary_symptom,product_rank)
            ) DEFAULT CHARSET=utf8mb4
        """)
        cur.execute(f"DROP TABLE IF EXISTS {quote_ident(INGREDIENT_TABLE + '_next')}")
        cur.execute(f"""
            CREATE TABLE {quote_ident(INGREDIENT_TABLE + '_next')} (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              primary_symptom VARCHAR(64) NOT NULL, secondary_symptom VARCHAR(64) NOT NULL,
              ingredient_rank INT NOT NULL, standard_ingredient_id VARCHAR(64) NOT NULL,
              standard_name VARCHAR(255) NOT NULL, ingredient_family VARCHAR(128) NULL,
              source_type VARCHAR(64) NULL, animal_source VARCHAR(128) NULL,
              primary_nutrition_role VARCHAR(128) NULL,
              supporting_brand_count INT NOT NULL, supporting_product_count INT NOT NULL,
              supporting_formula_count INT NOT NULL, supporting_clue_count INT NOT NULL,
              improvement_count INT NOT NULL, worsening_count INT NOT NULL,
              uncertain_count INT NOT NULL, improvement_rate DECIMAL(10,4) NULL,
              improvement_sample INT NOT NULL, average_position DECIMAL(10,2) NOT NULL,
              representative_score DECIMAL(12,4) NOT NULL,
              evidence_scope VARCHAR(64) NOT NULL, generated_at DATETIME NOT NULL,
              UNIQUE KEY uq_ingredient(secondary_symptom,standard_ingredient_id),
              KEY idx_rank(secondary_symptom,ingredient_rank)
            ) DEFAULT CHARSET=utf8mb4
        """)
    conn.commit()


def insert_rows(conn, table, rows):
    if not rows:
        raise RuntimeError(f"{table} 待写入数据为空，拒绝替换旧表")
    now = datetime.now()
    payload = []
    for row in rows:
        item = dict(row)
        item["generated_at"] = now
        payload.append(item)
    columns = list(payload[0])
    sql = (
        f"INSERT INTO {quote_ident(table + '_next')} "
        f"({','.join(quote_ident(c) for c in columns)}) VALUES "
        f"({','.join('%(' + c + ')s' for c in columns)})"
    )
    with conn.cursor() as cur:
        for offset in range(0, len(payload), 500):
            cur.executemany(sql, payload[offset:offset + 500])


def atomic_swap(conn):
    """先完整写入 next 表，再用 RENAME 原子换表，避免中途失败丢失旧数据。"""
    with conn.cursor() as cur:
        for table in (DETAIL_TABLE, PRODUCT_TABLE, INGREDIENT_TABLE):
            cur.execute(f"DROP TABLE IF EXISTS {quote_ident(table + '_old')}")
            cur.execute(f"SHOW TABLES LIKE %s", (table,))
            if cur.fetchone():
                cur.execute(
                    f"RENAME TABLE {quote_ident(table)} TO {quote_ident(table + '_old')}, "
                    f"{quote_ident(table + '_next')} TO {quote_ident(table)}"
                )
            else:
                cur.execute(f"RENAME TABLE {quote_ident(table + '_next')} TO {quote_ident(table)}")
            cur.execute(f"DROP TABLE IF EXISTS {quote_ident(table + '_old')}")
    conn.commit()


def write_outputs(conn, detail_rows, product_rows, ingredient_rows):
    ensure_tables(conn)
    try:
        insert_rows(conn, DETAIL_TABLE, detail_rows)
        insert_rows(conn, PRODUCT_TABLE, product_rows)
        insert_rows(conn, INGREDIENT_TABLE, ingredient_rows)
        conn.commit()
        atomic_swap(conn)
    except Exception:
        conn.rollback()
        raise


def parse_args():
    parser = argparse.ArgumentParser(description="构建病症代表产品和代表原料")
    parser.add_argument("--dry-run", action="store_true", help="只计算预览，不写库")
    parser.add_argument(
        "--clues-connection",
        choices=("ssh", "direct"),
        default="ssh",
        help="病症线索库连接方式：开发机默认 ssh，生产 API 使用 direct",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    local_conn = local_connection()
    try:
        print("[1/4] 读取本地标准品牌与别名...")
        name_to_id, id_to_name = load_brand_master(local_conn)
        print("[2/4] 读取远程病症线索并标准化品牌...")
        with clues_connection(args.clues_connection) as remote_conn:
            disease_stats, unmatched = load_disease_brand_stats(
                remote_conn, name_to_id, id_to_name
            )
        print(
            f"  病症×标准品牌: {len(disease_stats)}, "
            f"未匹配原始品牌: {len(unmatched)}"
        )
        print("[3/4] 关联当前有效配方与 catfood_formula_ingredient_item...")
        chain_by_brand = load_formula_ingredient_chain(local_conn)
        detail_rows, product_rows, ingredient_rows = build_outputs(disease_stats, chain_by_brand)
        print(
            f"  明细: {len(detail_rows)}, 代表产品: {len(product_rows)}, "
            f"代表原料: {len(ingredient_rows)}"
        )
        if args.dry_run:
            print("[4/4] dry-run，未写入数据库")
        else:
            print("[4/4] 原子替换三张本地结果表...")
            write_outputs(local_conn, detail_rows, product_rows, ingredient_rows)
            print("写入完成")

        print("\n每个病症的 TOP3 代表产品：")
        for row in sorted(product_rows, key=lambda x: (x["secondary_symptom"], x["product_rank"])):
            if row["product_rank"] <= 3:
                rate = "-" if row["improvement_rate"] is None else f"{row['improvement_rate']:.1%}"
                print(
                    f"  {row['secondary_symptom']} #{row['product_rank']} "
                    f"{row['brand_name']}·{row['product_name']} "
                    f"(品牌线索={row['disease_clue_count']}, 改善率={rate})"
                )
        print("\n每个病症的 TOP5 代表原料：")
        for row in sorted(ingredient_rows, key=lambda x: (x["secondary_symptom"], x["ingredient_rank"])):
            if row["ingredient_rank"] <= 5:
                print(
                    f"  {row['secondary_symptom']} #{row['ingredient_rank']} "
                    f"{row['standard_name']} (品牌={row['supporting_brand_count']}, "
                    f"产品={row['supporting_product_count']})"
                )
    finally:
        local_conn.close()


if __name__ == "__main__":
    main()
