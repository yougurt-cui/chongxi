#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pymysql


DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", os.getenv("DB_HOST", "127.0.0.1")),
    "port": int(os.getenv("MYSQL_PORT", os.getenv("DB_PORT", "3306"))),
    "user": os.getenv("MYSQL_USER", os.getenv("DB_USER", "root")),
    "password": os.getenv("MYSQL_PASSWORD", os.getenv("DB_PASSWORD", "")),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
}

SOURCE_DB = os.getenv("PROCESS_SIGNAL_SOURCE_DB", "csv_labeling")
OUTPUT_DB = os.getenv("PROCESS_SIGNAL_OUTPUT_DB", "csv_labeling")
OUTPUT_TABLE = os.getenv("PROCESS_SIGNAL_OUTPUT_TABLE", "catfood_process_signal_candidates")
SOURCE_TABLES = ("xiaohongshu_raw_comments", "douyin_raw_comments")
ARTIFACT_ROOT = Path(__file__).resolve().parent / "catfood_process_signal_artifacts"
BATCH_SIZE = 1000

CATFOOD_CONTEXT_PATTERN = re.compile(
    "猫粮|主粮|干粮|湿粮|冻干粮|风干粮|烘焙粮|处方粮|幼猫粮|成猫粮|减肥粮|泌尿粮|肠胃粮|"
    "猫|猫咪|喵|主子|毛孩子|布偶|德文|英短|美短|缅因|"
    "皇家|冠能|领先|渴望|爱肯拿|百利|鲜朗|素力高|金素|发米娜|法米娜|弗列加特|费列加特|"
    "伯纳天纯|网易严选|江小傲|阿飞和巴弟|帕特|巅峰|滋益巅峰|ZIWI|K9|麦富迪|顽皮|"
    "乖宝|海洋之星|比瑞吉|耐威克|高爷家|坦克小希|诚实一口|鲜肉主义|蓝馔|纽顿|GO|NOW|"
    "纽翠斯|自然光|天衡宝|希尔斯|普瑞纳|醇粹|虎太郎",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class SourceSpec:
    table: str
    platform: str
    platform_col: str
    id_col: str
    external_id_col: str
    title_col: str
    content_col: str
    like_col: str
    time_col: str
    keyword_col: str


@dataclass(frozen=True)
class ProcessRule:
    category: str
    tag: str
    expressions: tuple[str, ...]
    qc_items: tuple[str, ...]
    confidence: str = "high"
    require_any: tuple[str, ...] = ()
    exclude_any: tuple[str, ...] = ()


SOURCE_SPECS = {
    "xiaohongshu_raw_comments": SourceSpec(
        table="xiaohongshu_raw_comments",
        platform="xiaohongshu",
        platform_col="",
        id_col="id",
        external_id_col="external_id",
        title_col="title",
        content_col="content",
        like_col="like_count",
        time_col="comment_time",
        keyword_col="query_keyword",
    ),
    "douyin_raw_comments": SourceSpec(
        table="douyin_raw_comments",
        platform="douyin",
        platform_col="",
        id_col="id",
        external_id_col="external_id",
        title_col="post_title",
        content_col="post_content",
        like_col="post_like_count",
        time_col="comment_date",
        keyword_col="search_keyword",
    ),
    "catfood_brand_health_candidates": SourceSpec(
        table="catfood_brand_health_candidates",
        platform="catfood_candidates",
        platform_col="platform",
        id_col="raw_id",
        external_id_col="external_id",
        title_col="title",
        content_col="content",
        like_col="",
        time_col="event_date",
        keyword_col="keyword",
    ),
}


RULES: tuple[ProcessRule, ...] = (
    ProcessRule("批次变化", "复购前后差异", ("以前吃没事", "之前吃没问题", "这次买的有问题", "以前不拉，这次拉了"), ("批次留样对比", "水分", "酸价", "过氧化值", "颗粒硬度", "粉化率")),
    ProcessRule("批次变化", "批次稳定性异常", ("这批粮", "这批猫粮", "这批有问题", "这一袋有问题", "这一箱有问题", "新批次"), ("批次留样对比", "水分", "酸价", "过氧化值", "颗粒硬度", "粉化率")),
    ProcessRule("批次变化", "包装/版本变更可疑", ("换包装后", "新版", "升级后", "改版后"), ("批次留样对比", "水分", "酸价", "过氧化值", "颗粒硬度", "粉化率")),
    ProcessRule("批次变化", "感官批次差异", ("颜色变了", "味道变了", "颗粒变了", "比以前油", "比以前硬"), ("批次留样对比", "水分", "酸价", "过氧化值", "颗粒硬度", "粉化率")),
    ProcessRule("表面油脂", "表面油脂残留可疑", ("颗粒很油", "粮很油", "摸起来油", "油乎乎", "油腻"), ("表面油脂残留", "后喷涂比例", "喷涂均匀度", "酸价", "过氧化值"), require_any=("粮", "猫粮", "颗粒", "袋", "碗", "嘴", "下巴", "吃完", "这款", "它家", "他家")),
    ProcessRule("表面油脂", "后喷涂控制可疑", ("碗底有油", "袋子里有油", "颗粒发亮", "有些颗粒特别油"), ("表面油脂残留", "后喷涂比例", "喷涂均匀度", "酸价", "过氧化值")),
    ProcessRule("表面油脂", "接触性油脂风险", ("吃完下巴油", "下巴油油的", "嘴边油", "下巴毛油"), ("表面油脂残留", "后喷涂比例", "喷涂均匀度", "酸价", "过氧化值")),
    ProcessRule("表面油脂", "批次油脂波动可疑", ("这批更油", "这次特别油", "比以前油很多"), ("表面油脂残留", "后喷涂比例", "喷涂均匀度", "酸价", "过氧化值")),
    ProcessRule("气味氧化", "油脂氧化风险", ("哈喇味", "油耗味", "油味怪", "油味重"), ("酸价", "过氧化值", "水分", "水活度", "包装密封性", "挥发性气味指标")),
    ProcessRule("气味氧化", "气味刺激可疑", ("味道很冲", "味道刺鼻", "香精味重", "诱食剂味"), ("酸价", "过氧化值", "水分", "水活度", "包装密封性", "挥发性气味指标")),
    ProcessRule("气味氧化", "新鲜度异常", ("不新鲜", "变质味", "酸味", "怪味"), ("酸价", "过氧化值", "水分", "水活度", "包装密封性", "挥发性气味指标"), require_any=("粮", "猫粮", "颗粒", "袋", "猫条", "罐头", "冻干", "这款", "它家", "他家", "打开")),
    ProcessRule("气味氧化", "仓储稳定性可疑", ("霉味", "受潮味", "闷味"), ("酸价", "过氧化值", "水分", "水活度", "包装密封性", "挥发性气味指标")),
    ProcessRule("颗粒物性", "颗粒硬度异常", ("颗粒太硬", "很硬", "咬不动", "猫不嚼"), ("颗粒硬度", "颗粒尺寸分布", "颗粒均匀度", "膨化密度", "水分"), require_any=("粮", "猫粮", "颗粒", "吃", "嚼", "咬", "这款", "它家", "他家")),
    ProcessRule("颗粒物性", "颗粒尺寸不适配", ("颗粒太大", "颗粒太小", "卡嗓子", "噎住"), ("颗粒硬度", "颗粒尺寸分布", "颗粒均匀度", "膨化密度", "水分")),
    ProcessRule("颗粒物性", "颗粒均匀度异常", ("颗粒不均匀", "大小不一", "有大有小"), ("颗粒硬度", "颗粒尺寸分布", "颗粒均匀度", "膨化密度", "水分"), require_any=("粮", "猫粮", "颗粒", "袋", "这款", "它家", "他家")),
    ProcessRule("颗粒物性", "消化适配可疑", ("吐整粒", "吐出来还是完整颗粒", "便便里有整粒", "拉出来还有粮", "直接吞"), ("颗粒硬度", "颗粒尺寸分布", "颗粒均匀度", "膨化密度", "水分")),
    ProcessRule("粉化碎渣", "粉化率偏高", ("粉很多", "粉末多", "袋底全是粉", "底下都是粉"), ("粉化率", "颗粒强度", "运输破碎率", "包装抗压性")),
    ProcessRule("粉化碎渣", "颗粒强度不足", ("一捏就碎", "容易碎", "碎渣多", "掉渣"), ("粉化率", "颗粒强度", "运输破碎率", "包装抗压性")),
    ProcessRule("粉化碎渣", "运输破碎可疑", ("到手碎了", "打开全是渣", "运输后碎很多"), ("粉化率", "颗粒强度", "运输破碎率", "包装抗压性")),
    ProcessRule("粉化碎渣", "粉尘刺激可疑", ("粉尘大", "猫吃的时候呛", "吃的时候咳"), ("粉化率", "颗粒强度", "运输破碎率", "包装抗压性")),
    ProcessRule("水分受潮", "水分控制异常", ("粮软", "不脆", "潮潮的", "有点潮"), ("水分", "水活度", "包装密封性", "微生物指标", "霉菌毒素"), require_any=("粮", "猫粮", "颗粒", "袋", "这款", "它家", "他家")),
    ProcessRule("水分受潮", "包装密封可疑", ("受潮", "袋子漏气", "封口不好"), ("水分", "水活度", "包装密封性", "微生物指标", "霉菌毒素"), require_any=("粮", "猫粮", "颗粒", "袋", "包装", "这款", "它家", "他家")),
    ProcessRule("水分受潮", "霉变风险", ("发霉", "霉味"), ("水分", "水活度", "包装密封性", "微生物指标", "霉菌毒素"), require_any=("粮", "猫粮", "颗粒", "袋", "包装", "猫条", "罐头", "冻干", "这款", "它家", "他家", "打开")),
    ProcessRule("水分受潮", "结块异常", ("结块", "黏在一起", "一坨一坨"), ("水分", "水活度", "包装密封性", "微生物指标", "霉菌毒素"), require_any=("粮", "猫粮", "颗粒", "袋", "这款", "它家", "他家")),
    ProcessRule("熟化消化", "熟化度不足可疑", ("不消化", "便便里有颗粒", "没消化完"), ("淀粉糊化度", "蛋白消化率", "颗粒硬度", "膨化温度", "膨化压力", "水分")),
    ProcessRule("熟化消化", "淀粉糊化不足可疑", ("拉出来有粮", "吃进去什么样出来什么样"), ("淀粉糊化度", "蛋白消化率", "颗粒硬度", "膨化温度", "膨化压力", "水分")),
    ProcessRule("熟化消化", "蛋白消化率波动可疑", ("便臭", "胀气", "软便明显"), ("淀粉糊化度", "蛋白消化率", "颗粒硬度", "膨化温度", "膨化压力", "水分"), confidence="low", require_any=("粮", "猫粮", "颗粒", "这款", "这批", "这次", "吃完", "吃了")),
    ProcessRule("熟化消化", "胃肠刺激可疑", ("吃完马上吐", "吃完马上拉"), ("淀粉糊化度", "蛋白消化率", "颗粒硬度", "膨化温度", "膨化压力", "水分"), confidence="medium"),
    ProcessRule("适口喷涂", "适口喷涂刺激可疑", ("味道很冲", "香精味重", "诱食剂味"), ("喷涂比例", "喷涂均匀度", "挥发性气味指标", "适口剂稳定性")),
    ProcessRule("适口喷涂", "喷涂强度偏高可疑", ("猫很上头", "吃太快", "吃完就吐"), ("喷涂比例", "喷涂均匀度", "挥发性气味指标", "适口剂稳定性"), confidence="medium"),
    ProcessRule("适口喷涂", "喷涂均匀性可疑", ("有些颗粒特别油", "有些没味", "有些颗粒味道重"), ("喷涂比例", "喷涂均匀度", "挥发性气味指标", "适口剂稳定性")),
    ProcessRule("适口喷涂", "适口性波动可疑", ("之前爱吃，这次不吃", "以前吃，这次不碰"), ("喷涂比例", "喷涂均匀度", "挥发性气味指标", "适口剂稳定性")),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter raw comments into low-false-positive catfood process signal candidates.")
    parser.add_argument("--source-db", default=SOURCE_DB)
    parser.add_argument("--output-db", default=OUTPUT_DB)
    parser.add_argument("--output-table", default=OUTPUT_TABLE)
    parser.add_argument("--output-dir", default=str(ARTIFACT_ROOT))
    parser.add_argument("--if-exists", choices=("replace", "append", "fail"), default="replace")
    parser.add_argument("--limit", type=int, default=0, help="debug limit per source table; 0 means all rows")
    return parser.parse_args()


def connect_mysql(cursorclass=pymysql.cursors.DictCursor):
    return pymysql.connect(**DB_CONFIG, cursorclass=cursorclass)


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"Unsafe identifier: {name}")
    return f"`{name}`"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def selected_columns(spec: SourceSpec) -> str:
    cols = [
        spec.id_col,
        spec.external_id_col,
        spec.title_col,
        spec.content_col,
        spec.time_col,
        spec.keyword_col,
        "comment_text",
    ]
    if spec.like_col:
        cols.append(spec.like_col)
    if spec.platform_col:
        cols.append(spec.platform_col)
    return ", ".join(quote_ident(col) for col in cols if col)


def create_output_table(conn, output_db: str, output_table: str, if_exists: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {quote_ident(output_db)} DEFAULT CHARACTER SET utf8mb4")
        exists_sql = """
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """
        cur.execute(exists_sql, (output_db, output_table))
        exists = bool(next(iter(cur.fetchone().values())))
        if exists and if_exists == "fail":
            raise ValueError(f"output table already exists: {output_db}.{output_table}")
        if exists and if_exists == "replace":
            cur.execute(f"DROP TABLE {quote_ident(output_db)}.{quote_ident(output_table)}")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(output_db)}.{quote_ident(output_table)} (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              run_id VARCHAR(64) NOT NULL,
              source_platform VARCHAR(32) NOT NULL,
              source_schema VARCHAR(64) NOT NULL,
              source_table VARCHAR(64) NOT NULL,
              source_row_id BIGINT NULL,
              external_id VARCHAR(255) NULL,
              has_process_signal TINYINT(1) NOT NULL DEFAULT 1,
              process_signal_category VARCHAR(64) NOT NULL,
              process_signal_tag VARCHAR(128) NOT NULL,
              matched_expression VARCHAR(128) NOT NULL,
              evidence_text VARCHAR(500) NOT NULL,
              recommended_qc_items VARCHAR(500) NOT NULL,
              confidence_level VARCHAR(32) NOT NULL,
              comment_text LONGTEXT NOT NULL,
              normalized_text LONGTEXT NOT NULL,
              source_title TEXT NULL,
              source_content MEDIUMTEXT NULL,
              source_like_count INT NULL,
              source_comment_time VARCHAR(64) NULL,
              source_keyword VARCHAR(255) NULL,
              inserted_at DATETIME NOT NULL,
              KEY idx_source (source_platform, source_row_id),
              KEY idx_category_tag (process_signal_category, process_signal_tag),
              KEY idx_confidence (confidence_level),
              KEY idx_keyword (source_keyword)
            ) DEFAULT CHARSET=utf8mb4
            """
        )
    conn.commit()


def iter_source_rows(source_db: str, spec: SourceSpec, limit: int = 0) -> Iterable[dict[str, Any]]:
    conn = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.SSDictCursor)
    try:
        sql = (
            f"SELECT {selected_columns(spec)} "
            f"FROM {quote_ident(source_db)}.{quote_ident(spec.table)} "
            f"WHERE comment_text IS NOT NULL AND TRIM(comment_text) <> ''"
        )
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
        with conn.cursor() as cur:
            cur.execute(sql)
            for row in cur:
                yield row
    finally:
        conn.close()


def contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value and value in text for value in values)


def evidence_snippet(text: str, expression: str, radius: int = 42) -> str:
    pos = text.find(expression)
    if pos < 0:
        return text[: radius * 2]
    start = max(0, pos - radius)
    end = min(len(text), pos + len(expression) + radius)
    return text[start:end]


def is_negated_match(text: str, expression: str) -> bool:
    if expression.startswith(("不", "没", "无")):
        return False
    pos = text.find(expression)
    if pos < 0:
        return False
    prefix = text[max(0, pos - 4) : pos]
    return any(marker in prefix for marker in ("不", "没", "没有", "不会", "无"))


def match_process_signals(text: str) -> list[tuple[ProcessRule, str, str]]:
    matches: list[tuple[ProcessRule, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for rule in RULES:
        if rule.require_any and not contains_any(text, rule.require_any):
            continue
        if rule.exclude_any and contains_any(text, rule.exclude_any):
            continue
        for expression in rule.expressions:
            if expression not in text:
                continue
            if is_negated_match(text, expression):
                continue
            key = (rule.category, rule.tag, expression)
            if key in seen:
                continue
            seen.add(key)
            matches.append((rule, expression, evidence_snippet(text, expression)))
    return matches


def has_catfood_context(row: dict[str, Any], spec: SourceSpec) -> bool:
    context = " ".join(
        normalize_text(row.get(col))
        for col in (spec.title_col, spec.content_col, spec.keyword_col, "comment_text")
    )
    return bool(CATFOOD_CONTEXT_PATTERN.search(context))


def build_output_row(
    run_id: str,
    source_db: str,
    spec: SourceSpec,
    row: dict[str, Any],
    rule: ProcessRule,
    expression: str,
    evidence: str,
) -> dict[str, Any]:
    text = normalize_text(row.get("comment_text"))
    platform = normalize_text(row.get(spec.platform_col)) if spec.platform_col else ""
    return {
        "run_id": run_id,
        "source_platform": platform or spec.platform,
        "source_schema": source_db,
        "source_table": spec.table,
        "source_row_id": row.get(spec.id_col),
        "external_id": normalize_text(row.get(spec.external_id_col)) or None,
        "has_process_signal": 1,
        "process_signal_category": rule.category,
        "process_signal_tag": rule.tag,
        "matched_expression": expression,
        "evidence_text": evidence,
        "recommended_qc_items": "、".join(rule.qc_items),
        "confidence_level": rule.confidence,
        "comment_text": row.get("comment_text") or "",
        "normalized_text": text,
        "source_title": row.get(spec.title_col),
        "source_content": row.get(spec.content_col),
        "source_like_count": row.get(spec.like_col),
        "source_comment_time": normalize_text(row.get(spec.time_col)) or None,
        "source_keyword": normalize_text(row.get(spec.keyword_col)) or None,
        "inserted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def insert_batch(conn, output_db: str, output_table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = f"""
        INSERT INTO {quote_ident(output_db)}.{quote_ident(output_table)} (
          run_id, source_platform, source_schema, source_table, source_row_id,
          external_id, has_process_signal, process_signal_category, process_signal_tag,
          matched_expression, evidence_text, recommended_qc_items, confidence_level,
          comment_text, normalized_text, source_title, source_content, source_like_count,
          source_comment_time, source_keyword, inserted_at
        )
        VALUES (
          %(run_id)s, %(source_platform)s, %(source_schema)s, %(source_table)s, %(source_row_id)s,
          %(external_id)s, %(has_process_signal)s, %(process_signal_category)s, %(process_signal_tag)s,
          %(matched_expression)s, %(evidence_text)s, %(recommended_qc_items)s, %(confidence_level)s,
          %(comment_text)s, %(normalized_text)s, %(source_title)s, %(source_content)s, %(source_like_count)s,
          %(source_comment_time)s, %(source_keyword)s, %(inserted_at)s
        )
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "source_platform",
        "source_row_id",
        "external_id",
        "process_signal_category",
        "process_signal_tag",
        "matched_expression",
        "evidence_text",
        "recommended_qc_items",
        "confidence_level",
        "comment_text",
        "source_title",
        "source_like_count",
        "source_comment_time",
        "source_keyword",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main_from_args(args: argparse.Namespace) -> dict[str, Any]:
    run_id = "catfood_process_signals_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    run_dir = Path(args.output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    output_conn = connect_mysql()
    try:
        create_output_table(output_conn, args.output_db, args.output_table, args.if_exists)
        all_matches: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        summary: dict[str, dict[str, int]] = {}
        category_counts: dict[str, int] = {}
        tag_counts: dict[str, int] = {}

        source_tables = tuple(getattr(args, "source_tables", None) or SOURCE_TABLES)
        unknown_tables = [table for table in source_tables if table not in SOURCE_SPECS]
        if unknown_tables:
            raise ValueError(f"unsupported source tables: {', '.join(unknown_tables)}")

        for table in source_tables:
            spec = SOURCE_SPECS[table]
            scanned = 0
            matched_comments = 0
            matched_rows = 0
            for row in iter_source_rows(args.source_db, spec, args.limit):
                scanned += 1
                if not has_catfood_context(row, spec):
                    continue
                text = normalize_text(row.get("comment_text"))
                matches = match_process_signals(text)
                if not matches:
                    continue
                matched_comments += 1
                for rule, expression, evidence in matches:
                    output_row = build_output_row(run_id, args.source_db, spec, row, rule, expression, evidence)
                    pending.append(output_row)
                    all_matches.append(output_row)
                    matched_rows += 1
                    category_counts[rule.category] = category_counts.get(rule.category, 0) + 1
                    tag_counts[f"{rule.category}/{rule.tag}"] = tag_counts.get(f"{rule.category}/{rule.tag}", 0) + 1
                    if len(pending) >= BATCH_SIZE:
                        insert_batch(output_conn, args.output_db, args.output_table, pending)
                        pending.clear()
            summary[table] = {"scanned": scanned, "matched_comments": matched_comments, "matched_signal_rows": matched_rows}

        insert_batch(output_conn, args.output_db, args.output_table, pending)
        csv_path = run_dir / "catfood_process_signals.csv"
        write_csv(csv_path, all_matches)

        summary_payload = {
            "run_id": run_id,
            "source_db": args.source_db,
            "source_tables": source_tables,
            "output_table": f"{args.output_db}.{args.output_table}",
            "if_exists": args.if_exists,
            "matched_signal_rows": len(all_matches),
            "summary": summary,
            "category_counts": category_counts,
            "tag_counts": tag_counts,
            "csv": str(csv_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        (run_dir / "summary.json").write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"run_id={run_id}")
        print(f"output_table={args.output_db}.{args.output_table}")
        print(f"matched_signal_rows={len(all_matches)}")
        for table, item in summary.items():
            print(
                f"{table}: scanned={item['scanned']} "
                f"matched_comments={item['matched_comments']} matched_signal_rows={item['matched_signal_rows']}"
            )
        print(f"csv={csv_path}")
        print(f"summary={run_dir / 'summary.json'}")
        return summary_payload
    finally:
        output_conn.close()


def main() -> None:
    main_from_args(parse_args())


if __name__ == "__main__":
    main()
