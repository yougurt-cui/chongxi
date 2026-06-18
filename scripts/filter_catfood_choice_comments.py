#!/usr/bin/env python
"""Incrementally filter raw comments that contain cat-food choice intent."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


SOURCE_TABLES = ("xiaohongshu_raw_comments", "douyin_raw_comments")
OUTPUT_TABLE = "catfood_choice_comments_filtered"
ARTIFACT_ROOT = BASE_DIR / "var" / "catfood_choice_comment_artifacts"
BATCH_SIZE = 1000

CATFOOD_TERMS = (
    "猫粮|主粮|干粮|湿粮|冻干粮|风干粮|烘焙粮|处方粮|幼猫粮|成猫粮|减肥粮|泌尿粮|肠胃粮|粮"
)
CAT_TERMS = (
    "猫|咪|喵|崽|主子|毛孩子|布偶|德文|英短|美短|缅因|幼猫|成猫|绝育|玻璃胃|软便|拉稀|便秘|泌尿|结石|黑下巴"
)
BRAND_TERMS = (
    "皇家|冠能|领先|渴望|爱肯拿|百利|鲜朗|素力高|金素|发米娜|弗列加特|费列加特|伯纳天纯|网易严选|"
    "江小傲|阿飞和巴弟|帕特|巅峰|滋益巅峰|ZIWI|K9|麦富迪|顽皮|乖宝|海洋之星|比瑞吉|耐威克|"
    "高爷家|坦克小希|诚实一口|鲜肉主义|蓝馔|纽顿|GO|NOW|纽翠斯|自然光|天衡宝|希尔斯|普瑞纳|醇粹|虎太郎"
)
CHOICE_TERMS = (
    "推荐|求推|求推荐|求安利|求链接|安利|有没有|有无|哪款|哪个|哪种|哪家|什么牌子|吃什么|买什么|"
    "什么粮|换什么|怎么选|如何选|怎么挑|如何挑|选什么|选哪个|选哪款|哪个好|哪个牌子|适合吗|"
    "可以吗|能吃吗|能喂吗|靠谱不|不翻车|避雷|踩雷|纠结|二选一|对比|vs|VS|平替|"
    "性价比|预算|试吃装|蹲一个|蹲|求助|请问|想问|问问"
)
SPECIAL_NEED_TERMS = (
    "软便|拉稀|便秘|呕吐|肠胃|玻璃胃|黑下巴|过敏|泌尿|结石|肾|尿闭|减肥|长肉|美毛|绝育|"
    "幼猫|成猫|老年|布偶|德文|英短|美短|缅因"
)
ADVICE_TERMS = (
    "选.*粮|粮.*选|不要.*粮|少吃.*粮|适合.*(猫|咪|幼猫|成猫|布偶|德文)|推荐.*粮|粮.*推荐"
)
NOISE_TERMS = (
    "托盘|碗架|饭碗|水碗|猫砂盆|猫砂铲|猫窝|猫抓板|航空箱|背包|玩具|梳子|剪指甲|饮水机|过滤芯"
)


@dataclass(frozen=True)
class SourceSpec:
    table: str
    platform: str
    id_col: str
    external_id_col: str
    title_col: str
    content_col: str
    like_col: str
    time_col: str
    keyword_col: str


SOURCE_SPECS = {
    "xiaohongshu_raw_comments": SourceSpec(
        table="xiaohongshu_raw_comments",
        platform="xiaohongshu",
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
        id_col="id",
        external_id_col="external_id",
        title_col="post_title",
        content_col="post_content",
        like_col="post_like_count",
        time_col="comment_date",
        keyword_col="search_keyword",
    ),
}


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"Unsafe identifier: {name}")
    return f"`{name}`"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def re_search(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def find_named_signals(text: str) -> tuple[list[str], list[str], int]:
    signals: list[str] = []
    intents: list[str] = []
    score = 0
    has_catfood_term = re_search(CATFOOD_TERMS, text)
    has_cat_term = re_search(CAT_TERMS, text)
    has_brand = re_search(BRAND_TERMS, text)
    has_choice = re_search(CHOICE_TERMS, text)
    has_special_need = re_search(SPECIAL_NEED_TERMS, text)
    has_advice = re_search(ADVICE_TERMS, text)
    has_noise = re_search(NOISE_TERMS, text)

    if has_catfood_term:
        signals.append("猫粮/粮食语境")
        score += 2
    if has_cat_term:
        signals.append("猫/症状语境")
        score += 1
    if has_brand:
        signals.append("猫粮品牌")
        score += 2
    if has_choice:
        signals.append("选择/推荐表达")
        score += 3
    if has_special_need:
        signals.append("特殊需求/症状")
        score += 1
    if has_advice:
        signals.append("选粮建议表达")
        score += 2
    if has_noise:
        signals.append("疑似非猫粮物品")
        score -= 2

    if re_search("推荐|求推|求推荐|求安利|有没有|有无|蹲", text):
        intents.append("求推荐")
    if re_search("哪款|哪个|哪种|哪家|怎么选|如何选|怎么挑|如何挑|选什么|选哪个|选哪款|哪个好|哪个牌子|纠结|二选一|对比|vs|VS", text):
        intents.append("品牌/款式对比")
    if re_search("换粮|想换|换什么|不吃|爱吃|适口|试吃装", text):
        intents.append("换粮/适口性")
    if has_special_need:
        intents.append("特殊需求选粮")
    if re_search("性价比|预算|平替|贵|便宜|活动|打折", text):
        intents.append("价格/平替")
    if re_search("求链接|链接|官旗|旗舰|真假|正品|哪里买|哪买", text):
        intents.append("购买渠道")
    if has_advice and not intents:
        intents.append("选粮建议")
    return signals, intents, score


def is_choice_comment(comment_text: Any) -> tuple[bool, list[str], list[str], int]:
    text = normalize_text(comment_text)
    if not text:
        return False, [], [], 0
    signals, intents, score = find_named_signals(text)
    has_catfood_context = (
        "猫粮/粮食语境" in signals
        or "猫粮品牌" in signals
        or (
            "猫/症状语境" in signals
            and re_search("什么牌子|买什么|换什么|哪款|哪个好|哪个牌子|怎么选|如何选|选.*粮|粮.*选", text)
        )
    )
    has_choice_signal = (
        "选择/推荐表达" in signals
        or "选粮建议表达" in signals
        or (re_search("适合|可以|能吃|能喂", text) and ("特殊需求/症状" in signals or "猫粮品牌" in signals))
    )
    if not has_catfood_context or not has_choice_signal:
        return False, signals, intents, score
    weak_only = re.fullmatch(r"(求链接|蹲一个|蹲|链接|哪买|哪里买)[。！？!?~～\s]*", text)
    if weak_only and not ("猫粮/粮食语境" in signals or "猫粮品牌" in signals):
        return False, signals, intents, score
    return score >= 5, signals, intents or ["猫粮选择"], score


def connect_mysql(cursorclass=pymysql.cursors.DictCursor):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, autocommit=False, cursorclass=cursorclass)


def selected_columns(spec: SourceSpec) -> str:
    cols = [
        spec.id_col,
        spec.external_id_col,
        spec.title_col,
        spec.content_col,
        spec.like_col,
        spec.time_col,
        spec.keyword_col,
        "comment_text",
    ]
    return ", ".join(quote_ident(col) for col in cols)


def ensure_output_table(conn, output_table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(output_table)} (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              run_id VARCHAR(64) NOT NULL,
              source_platform VARCHAR(32) NOT NULL,
              source_schema VARCHAR(64) NOT NULL,
              source_table VARCHAR(64) NOT NULL,
              source_row_id BIGINT NULL,
              external_id VARCHAR(255) NULL,
              comment_text LONGTEXT NOT NULL,
              normalized_text LONGTEXT NOT NULL,
              intent_labels VARCHAR(255) NOT NULL,
              matched_signals VARCHAR(500) NOT NULL,
              choice_score INT NOT NULL,
              source_title TEXT NULL,
              source_content MEDIUMTEXT NULL,
              source_like_count INT NULL,
              source_comment_time VARCHAR(64) NULL,
              source_keyword VARCHAR(255) NULL,
              inserted_at DATETIME NOT NULL,
              KEY idx_source (source_platform, source_row_id),
              KEY idx_score (choice_score),
              KEY idx_intent (intent_labels)
            ) DEFAULT CHARSET=utf8mb4
            """
        )
    conn.commit()


def max_processed_source_row_id(conn, output_table: str, spec: SourceSpec) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COALESCE(MAX(source_row_id), 0) AS max_id
            FROM {quote_ident(output_table)}
            WHERE source_platform = %s AND source_table = %s
            """,
            (spec.platform, spec.table),
        )
        row = cur.fetchone() or {}
    return int(row.get("max_id") or 0)


def iter_source_rows(spec: SourceSpec, min_source_row_id: int, limit: int = 0) -> Iterable[dict[str, Any]]:
    conn = connect_mysql(cursorclass=pymysql.cursors.SSDictCursor)
    try:
        sql = (
            f"SELECT {selected_columns(spec)} "
            f"FROM {quote_ident(spec.table)} "
            f"WHERE {quote_ident(spec.id_col)} > %s "
            f"AND comment_text IS NOT NULL AND TRIM(comment_text) <> '' "
            f"ORDER BY {quote_ident(spec.id_col)} ASC"
        )
        params: list[Any] = [min_source_row_id]
        if limit > 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                yield row
    finally:
        conn.close()


def build_output_row(run_id: str, spec: SourceSpec, row: dict[str, Any], signals: list[str], intents: list[str], score: int) -> dict[str, Any]:
    text = normalize_text(row.get("comment_text"))
    return {
        "run_id": run_id,
        "source_platform": spec.platform,
        "source_schema": get_mysql_config()["database"],
        "source_table": spec.table,
        "source_row_id": row.get(spec.id_col),
        "external_id": normalize_text(row.get(spec.external_id_col)) or None,
        "comment_text": row.get("comment_text") or "",
        "normalized_text": text,
        "intent_labels": "、".join(dict.fromkeys(intents)),
        "matched_signals": "、".join(dict.fromkeys(signals)),
        "choice_score": score,
        "source_title": row.get(spec.title_col),
        "source_content": row.get(spec.content_col),
        "source_like_count": row.get(spec.like_col),
        "source_comment_time": normalize_text(row.get(spec.time_col)) or None,
        "source_keyword": normalize_text(row.get(spec.keyword_col)) or None,
        "inserted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def insert_batch(conn, output_table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = f"""
        INSERT INTO {quote_ident(output_table)} (
          run_id, source_platform, source_schema, source_table, source_row_id,
          external_id, comment_text, normalized_text, intent_labels, matched_signals,
          choice_score, source_title, source_content, source_like_count,
          source_comment_time, source_keyword, inserted_at
        )
        VALUES (
          %(run_id)s, %(source_platform)s, %(source_schema)s, %(source_table)s, %(source_row_id)s,
          %(external_id)s, %(comment_text)s, %(normalized_text)s, %(intent_labels)s, %(matched_signals)s,
          %(choice_score)s, %(source_title)s, %(source_content)s, %(source_like_count)s,
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
        "intent_labels",
        "matched_signals",
        "choice_score",
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-table", default=OUTPUT_TABLE)
    parser.add_argument("--output-dir", default=str(ARTIFACT_ROOT))
    parser.add_argument("--limit", type=int, default=0, help="debug limit per source table; 0 means all new rows")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = "catfood_choice_comments_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    run_dir = Path(args.output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    output_conn = connect_mysql()
    try:
        ensure_output_table(output_conn, args.output_table)
        all_matches: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        summary: dict[str, dict[str, int]] = {}
        for table in SOURCE_TABLES:
            spec = SOURCE_SPECS[table]
            min_source_row_id = max_processed_source_row_id(output_conn, args.output_table, spec)
            scanned = 0
            matched = 0
            for row in iter_source_rows(spec, min_source_row_id, args.limit):
                scanned += 1
                keep, signals, intents, score = is_choice_comment(row.get("comment_text"))
                if not keep:
                    continue
                matched += 1
                output_row = build_output_row(run_id, spec, row, signals, intents, score)
                pending.append(output_row)
                all_matches.append(output_row)
                if not args.dry_run and len(pending) >= BATCH_SIZE:
                    insert_batch(output_conn, args.output_table, pending)
                    pending.clear()
            summary[table] = {
                "min_source_row_id": min_source_row_id,
                "scanned": scanned,
                "matched": matched,
            }
        if not args.dry_run:
            insert_batch(output_conn, args.output_table, pending)

        csv_path = run_dir / "catfood_choice_comments.csv"
        write_csv(csv_path, all_matches)
        summary_payload = {
            "run_id": run_id,
            "source_tables": SOURCE_TABLES,
            "output_table": args.output_table,
            "matched_total": len(all_matches),
            "summary": summary,
            "dry_run": bool(args.dry_run),
            "csv": str(csv_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary_payload, ensure_ascii=False, indent=2))
    finally:
        output_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
