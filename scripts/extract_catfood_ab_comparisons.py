#!/usr/bin/env python
"""Incrementally extract cat-food A/B comparison pairs from choice comments."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pymysql


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app_config import get_mysql_config  # noqa: E402


SOURCE_TABLE = "catfood_choice_comments_filtered"
OUTPUT_TABLE = "catfood_choice_ab_comparisons"
ARTIFACT_ROOT = BASE_DIR / "var" / "catfood_ab_comparison_artifacts"

PRODUCT_LEXICON = [
    "顽皮小金盾",
    "喵梵思紫金",
    "阿飞和巴弟",
    "网易严选",
    "伯纳天纯",
    "诚实一口",
    "高爷家",
    "坦克小希",
    "鲜肉主义",
    "海洋之星",
    "蓝氏猎兔",
    "蓝氏乳鸽",
    "皇家K36",
    "皇家k36",
    "皇家i27",
    "皇家I27",
    "爱肯拿",
    "弗列加特",
    "费列加特",
    "法明娜",
    "滋益巅峰",
    "法米娜",
    "发米娜",
    "绿福摩",
    "麦富迪",
    "江小傲",
    "大玛仕",
    "虎太郎",
    "猫奶糕",
    "蓝馔",
    "纽翠斯",
    "天衡宝",
    "希尔斯",
    "素力高",
    "比瑞吉",
    "耐威克",
    "自然光",
    "皇家",
    "冠能",
    "领先",
    "渴望",
    "百利",
    "鲜朗",
    "金素",
    "福摩",
    "纯福",
    "宠率",
    "聪颖",
    "星速",
    "星宴",
    "纯粹",
    "醇粹",
    "派得",
    "纽顿",
    "蓝氏",
    "帕特",
    "巅峰",
    "顽皮",
    "乖宝",
    "霸弗",
    "Baff",
    "澳龙",
    "宽福",
    "玫斯",
    "奥丁",
    "普瑞纳",
    "喵梵思",
    "好主人",
    "喜喵",
    "开饭乐",
    "一对伴",
    "鲜郎",
    "诚实",
    "BK",
    "bk",
    "barf",
    "BARF",
    "K36",
    "k36",
    "i27",
    "I27",
    "ZIWI",
    "ziwi",
    "K9",
    "NOW",
    "now",
    "GO",
    "go",
]

NORMALIZE_MAP = {
    "费列加特": "弗列加特",
    "发米娜": "法米娜",
    "法明娜": "法米娜",
    "鲜郎": "鲜朗",
    "bk": "BK",
    "barf": "BARF",
    "Baff": "霸弗",
    "ziwi": "ZIWI",
    "now": "NOW",
    "go": "GO",
    "皇家k36": "皇家K36",
    "皇家i27": "皇家I27",
    "k36": "K36",
    "i27": "I27",
}

PRODUCT_STOPWORDS = {
    "",
    "这个",
    "这款",
    "那个",
    "这两个",
    "这俩",
    "它",
    "他",
    "她",
    "哪个",
    "哪款",
    "什么",
    "什么猫粮",
    "猫粮",
    "主粮",
    "干粮",
    "湿粮",
    "处方粮",
    "猫",
    "猫咪",
    "小猫",
    "幼猫",
    "成猫",
}

INTENT_RE = re.compile(
    r"哪个|哪款|哪一个|选哪个|选哪款|选什么|选|选择|推荐|好一点|好些|哪个好|更好|"
    r"更适合|适合|区别|差别|对比|二选一|纠结|怎么样|咋样|可以吗|能吃吗|靠谱吗|靠谱不"
)
PAIR_CONNECTOR_RE = re.compile(r"(和|跟|与|及|以及|还是|or|OR|vs|VS|/|、|，|,)")
PAIR_STRONG_CONNECTOR_RE = re.compile(r"(和|跟|与|及|以及|还是|or|OR|vs|VS|/|、)")
NON_COMPARE_RE = re.compile(
    r"混合|混着|掺着|拌着|一起吃|可以混|怎么混|同时喂|换着吃|换着喂|定期换|都吃|都喂|"
    r"和.*混|混.*和|我喂.*和|喂的.*和|吃的.*和|买了.*和|换成这个|替换成这个|"
    r"拿来当零食|做浇头|只能.*和|原因|是因为|选.*是因为"
)
PAIR_DECISION_RE = re.compile(
    r"选|选择|哪个|哪款|哪个好|哪一个|推荐哪|纠结|二选一|对比|区别|差别|还是|vs|VS|"
    r"怎么样|咋样|可以吗|更适合|适合哪"
)
KNOWN_COMPOUND_RE = re.compile(r"阿飞和巴弟")

PAIR_PATTERNS = [
    (
        "a_connector_b_then_intent",
        re.compile(
            r"(?P<a>[\u4e00-\u9fffA-Za-z0-9%（）()&·.+-]{2,24})\s*"
            r"(?P<connector>和|跟|与|及|以及|/|、|vs|VS)\s*"
            r"(?P<b>[\u4e00-\u9fffA-Za-z0-9%（）()&·.+-]{2,24})"
            r"(?P<tail>[^。！？!?；;]{0,30}?"
            r"(?:哪个|哪款|哪一个|选|选择|推荐|好一点|哪个好|更好|更适合|适合|区别|差别|对比|二选一|纠结|怎么样|咋样|可以吗))"
        ),
    ),
    (
        "a_or_b",
        re.compile(
            r"(?P<a>[\u4e00-\u9fffA-Za-z0-9%（）()&·.+-]{2,24})\s*"
            r"(?P<connector>还是|or|OR|vs|VS)\s*"
            r"(?P<b>[\u4e00-\u9fffA-Za-z0-9%（）()&·.+-]{2,24})"
            r"(?P<tail>[^。！？!?；;]{0,20})"
        ),
    ),
    (
        "intent_before_a_connector_b",
        re.compile(
            r"(?:纠结|考虑|想在|原本在|在|请问|问问|求问|想问)[^。！？!?；;]{0,12}?"
            r"(?P<a>[\u4e00-\u9fffA-Za-z0-9%（）()&·.+-]{2,24})\s*"
            r"(?P<connector>和|跟|与|及|以及|/|、)\s*"
            r"(?P<b>[\u4e00-\u9fffA-Za-z0-9%（）()&·.+-]{2,24})"
            r"(?P<tail>[^。！？!?；;]{0,24})"
        ),
    ),
]


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise ValueError(f"Unsafe identifier: {name}")
    return f"`{name}`"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_product_fragment(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    text = re.sub(r"^[：:，,。！？!?；;\s]+", "", text)
    text = re.sub(r"[：:，,。！？!?；;\s]+$", "", text)
    text = re.sub(r"^(请问|问问|求问|想问|你好|主播|博主|姐妹|宝子|宝|我想问一下|想问一下|想知道|我想知道)", "", text)
    text = re.sub(r"^(最近|现在|目前|已经|原本|本来)?(考虑|纠结|想在|在|选|选择|吃|喂|买|换|推荐|看看)", "", text)
    text = re.sub(r"^(小幼猫|小猫|幼猫|成猫|猫咪|猫猫|布偶|德文|英短|美短|缅因)(吃|用|更适合|适合)?", "", text)
    text = re.sub(r"(里面|之间|这两个|这俩|两个|这3种|这三种|三种)$", "", text)
    text = re.sub(
        r"(哪个|哪款|哪一个|选哪个|选哪款|选什么|推荐|好一点|哪个好|更好|更适合|适合|区别|差别|对比|二选一|纠结|怎么样|咋样|可以吗|能吃吗|靠谱吗|靠谱不).*$",
        "",
        text,
    )
    text = text.strip(" ：:，,。！？!?；;（）()[]【】")
    lexicon_name = find_lexicon_name(text)
    if lexicon_name:
        return lexicon_name
    chunks = re.split(r"[，,。！？!?；;\s]", text)
    text = chunks[-1].strip() if chunks else text
    text = re.sub(r"(猫粮|主粮|干粮)$", "", text).strip()
    return text.strip(" ：:，,。！？!?；;（）()[]【】")


def normalize_product_name(name: str) -> str:
    clean = clean_product_fragment(name)
    return NORMALIZE_MAP.get(clean, clean)


def find_lexicon_name(fragment: str) -> str | None:
    for name in sorted(PRODUCT_LEXICON, key=len, reverse=True):
        if name in fragment:
            return name
    lower_fragment = fragment.lower()
    for name in sorted(PRODUCT_LEXICON, key=len, reverse=True):
        if re.fullmatch(r"[A-Za-z0-9]+", name) and name.lower() in lower_fragment:
            return name
    return None


def recognized_product_name(name: str) -> bool:
    return bool(find_lexicon_name(name))


def valid_product_name(name: str) -> bool:
    if not name or name in PRODUCT_STOPWORDS:
        return False
    if len(name) < 2 or len(name) > 24:
        return False
    if re.search(r"^(这个|这款|那个|哪|什么|有没|有没有|可以|适合|推荐|猫咪|小猫|幼猫|成猫)", name):
        return False
    if re.search(r"(时候|问题|情况|链接|官方|旗舰店|直播间|医生|医院|配料表|生产厂家|试吃装)$", name):
        return False
    return True


def find_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    for name in sorted(PRODUCT_LEXICON, key=len, reverse=True):
        flags = re.IGNORECASE if re.fullmatch(r"[A-Za-z0-9]+", name) else 0
        pattern = re.compile(re.escape(name), flags=flags)
        for match in pattern.finditer(text):
            mentions.append(
                {
                    "name": name,
                    "normalized": normalize_product_name(name),
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    mentions.sort(key=lambda item: (item["start"], -(item["end"] - item["start"])))
    non_overlapping: list[dict[str, Any]] = []
    for mention in mentions:
        if any(not (mention["end"] <= item["start"] or mention["start"] >= item["end"]) for item in non_overlapping):
            continue
        non_overlapping.append(mention)
    non_overlapping.sort(key=lambda item: item["start"])
    return non_overlapping


def extract_regex_pairs(text: str) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for pattern_name, pattern in PAIR_PATTERNS:
        for match in pattern.finditer(text):
            raw_a = match.group("a")
            raw_b = match.group("b")
            connector = match.group("connector")
            tail = match.groupdict().get("tail") or ""
            context = match.group(0)
            if connector in {"，", ","} and not INTENT_RE.search(tail):
                continue
            if NON_COMPARE_RE.search(context):
                continue
            local_mentions = find_mentions(context)
            if len({item["normalized"] for item in local_mentions}) > 2:
                continue
            product_a = normalize_product_name(raw_a)
            product_b = normalize_product_name(raw_b)
            if not valid_product_name(product_a) or not valid_product_name(product_b):
                continue
            if not recognized_product_name(product_a) or not recognized_product_name(product_b):
                continue
            if product_a == product_b:
                continue
            if KNOWN_COMPOUND_RE.search(f"{product_a}和{product_b}"):
                continue
            pairs.append(
                {
                    "product_a": product_a,
                    "product_b": product_b,
                    "raw_product_a": clean_product_fragment(raw_a),
                    "raw_product_b": clean_product_fragment(raw_b),
                    "comparison_phrase": context,
                    "matched_pattern": pattern_name,
                    "confidence": 0.82 if pattern_name != "a_or_b" else 0.88,
                }
            )
    return pairs


def extract_lexicon_pairs(text: str) -> list[dict[str, Any]]:
    mentions = find_mentions(text)
    if len(mentions) < 2:
        return []

    pairs: list[dict[str, Any]] = []
    for left, right in zip(mentions, mentions[1:]):
        if left["normalized"] == right["normalized"]:
            continue
        between = text[left["end"]:right["start"]]
        if len(between) > 12:
            continue
        if not PAIR_CONNECTOR_RE.search(between):
            continue
        if ("," in between or "，" in between) and not PAIR_STRONG_CONNECTOR_RE.search(between):
            continue

        context_start = max(0, left["start"] - 24)
        context_end = min(len(text), right["end"] + 36)
        context = text[context_start:context_end]
        after = text[right["end"]:context_end]
        before = text[context_start:left["start"]]
        if not (PAIR_DECISION_RE.search(context) or re.search(r"纠结|考虑|想在|原本在", before)):
            continue
        if NON_COMPARE_RE.search(context):
            continue
        if KNOWN_COMPOUND_RE.search(f"{left['name']}和{right['name']}"):
            continue
        local_mentions = [
            item
            for item in mentions
            if context_start <= item["start"] and item["end"] <= context_end
        ]
        if len({item["normalized"] for item in local_mentions}) > 2:
            continue
        phrase = text[left["start"]:context_end]
        if after:
            phrase = phrase[: min(len(phrase), right["end"] - left["start"] + 36)]
        pairs.append(
            {
                "product_a": left["normalized"],
                "product_b": right["normalized"],
                "raw_product_a": left["name"],
                "raw_product_b": right["name"],
                "comparison_phrase": normalize_text(phrase),
                "matched_pattern": "lexicon_adjacent",
                "confidence": 0.9,
            }
        )
    return pairs


def dedupe_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen = set()
    for pair in sorted(pairs, key=lambda item: (-float(item["confidence"]), -len(item["comparison_phrase"]))):
        key = (pair["product_a"], pair["product_b"])
        reverse_key = (pair["product_b"], pair["product_a"])
        if key in seen or reverse_key in seen:
            continue
        seen.add(key)
        deduped.append(pair)
    deduped.sort(key=lambda item: item["comparison_phrase"])
    return deduped


def extract_ab_pairs(text: Any) -> list[dict[str, Any]]:
    normalized = normalize_text(text)
    if not normalized:
        return []
    if not re.search(r"和|跟|与|及|以及|还是|or|OR|vs|VS|/|、|，|,|二选一|对比|纠结", normalized):
        return []
    pairs = extract_lexicon_pairs(normalized)
    pairs.extend(extract_regex_pairs(normalized))
    return dedupe_pairs(pairs)


def connect_mysql(cursorclass=pymysql.cursors.DictCursor):
    cfg = get_mysql_config()
    return pymysql.connect(**cfg, autocommit=False, cursorclass=cursorclass)


def ensure_output_table(conn, output_table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_ident(output_table)} (
              id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
              run_id VARCHAR(64) NOT NULL,
              source_choice_id BIGINT NOT NULL,
              source_platform VARCHAR(32) NOT NULL,
              source_schema VARCHAR(64) NOT NULL,
              source_table VARCHAR(64) NOT NULL,
              source_row_id BIGINT NULL,
              external_id VARCHAR(255) NULL,
              pair_index INT NOT NULL,
              product_a VARCHAR(128) NOT NULL,
              product_b VARCHAR(128) NOT NULL,
              raw_product_a VARCHAR(128) NOT NULL,
              raw_product_b VARCHAR(128) NOT NULL,
              comparison_phrase TEXT NOT NULL,
              matched_pattern VARCHAR(64) NOT NULL,
              confidence DECIMAL(5,2) NOT NULL,
              intent_labels VARCHAR(255) NULL,
              choice_score INT NULL,
              comment_text LONGTEXT NOT NULL,
              source_title TEXT NULL,
              source_comment_time VARCHAR(64) NULL,
              source_keyword VARCHAR(255) NULL,
              inserted_at DATETIME NOT NULL,
              KEY idx_pair (product_a, product_b),
              KEY idx_product_a (product_a),
              KEY idx_product_b (product_b),
              KEY idx_source_choice (source_choice_id),
              KEY idx_confidence (confidence)
            ) DEFAULT CHARSET=utf8mb4
            """
        )
    conn.commit()


def max_processed_source_choice_id(conn, output_table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COALESCE(MAX(source_choice_id), 0) AS max_id FROM {quote_ident(output_table)}")
        row = cur.fetchone() or {}
    return int(row.get("max_id") or 0)


def iter_choice_rows(source_table: str, min_source_choice_id: int, limit: int = 0) -> Iterable[dict[str, Any]]:
    conn = connect_mysql(cursorclass=pymysql.cursors.SSDictCursor)
    try:
        sql = f"""
            SELECT
              id,
              source_platform,
              source_schema,
              source_table,
              source_row_id,
              external_id,
              normalized_text,
              comment_text,
              intent_labels,
              choice_score,
              source_title,
              source_comment_time,
              source_keyword
            FROM {quote_ident(source_table)}
            WHERE id > %s
              AND (
                normalized_text REGEXP '和|跟|与|及|以及|还是|or|OR|vs|VS|/|、|，|,|二选一|对比|纠结'
                OR intent_labels LIKE %s
              )
            ORDER BY id ASC
        """
        params: list[Any] = [min_source_choice_id, "%品牌/款式对比%"]
        if limit > 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for row in cur:
                yield row
    finally:
        conn.close()


def build_output_row(run_id: str, source_row: dict[str, Any], pair_index: int, pair: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "source_choice_id": source_row["id"],
        "source_platform": source_row["source_platform"],
        "source_schema": source_row["source_schema"],
        "source_table": source_row["source_table"],
        "source_row_id": source_row["source_row_id"],
        "external_id": source_row.get("external_id"),
        "pair_index": pair_index,
        "product_a": pair["product_a"],
        "product_b": pair["product_b"],
        "raw_product_a": pair["raw_product_a"],
        "raw_product_b": pair["raw_product_b"],
        "comparison_phrase": pair["comparison_phrase"],
        "matched_pattern": pair["matched_pattern"],
        "confidence": pair["confidence"],
        "intent_labels": source_row.get("intent_labels"),
        "choice_score": source_row.get("choice_score"),
        "comment_text": source_row.get("comment_text") or "",
        "source_title": source_row.get("source_title"),
        "source_comment_time": source_row.get("source_comment_time"),
        "source_keyword": source_row.get("source_keyword"),
        "inserted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def insert_rows(conn, output_table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = f"""
        INSERT INTO {quote_ident(output_table)} (
          run_id, source_choice_id, source_platform, source_schema, source_table, source_row_id,
          external_id, pair_index, product_a, product_b, raw_product_a, raw_product_b,
          comparison_phrase, matched_pattern, confidence, intent_labels, choice_score,
          comment_text, source_title, source_comment_time, source_keyword, inserted_at
        )
        VALUES (
          %(run_id)s, %(source_choice_id)s, %(source_platform)s, %(source_schema)s, %(source_table)s, %(source_row_id)s,
          %(external_id)s, %(pair_index)s, %(product_a)s, %(product_b)s, %(raw_product_a)s, %(raw_product_b)s,
          %(comparison_phrase)s, %(matched_pattern)s, %(confidence)s, %(intent_labels)s, %(choice_score)s,
          %(comment_text)s, %(source_title)s, %(source_comment_time)s, %(source_keyword)s, %(inserted_at)s
        )
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "source_platform",
        "source_choice_id",
        "source_row_id",
        "pair_index",
        "product_a",
        "product_b",
        "raw_product_a",
        "raw_product_b",
        "confidence",
        "matched_pattern",
        "comparison_phrase",
        "intent_labels",
        "choice_score",
        "comment_text",
        "source_title",
        "source_comment_time",
        "source_keyword",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-table", default=SOURCE_TABLE)
    parser.add_argument("--output-table", default=OUTPUT_TABLE)
    parser.add_argument("--output-dir", default=str(ARTIFACT_ROOT))
    parser.add_argument("--since-source-choice-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0, help="debug limit; 0 means all eligible new rows")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_id = "catfood_ab_comparisons_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    run_dir = Path(args.output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_mysql()
    try:
        ensure_output_table(conn, args.output_table)
        min_source_choice_id = (
            int(args.since_source_choice_id)
            if args.since_source_choice_id is not None
            else max_processed_source_choice_id(conn, args.output_table)
        )
        output_rows: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        scanned = 0
        source_rows_with_pair = 0

        for source_row in iter_choice_rows(args.source_table, min_source_choice_id, args.limit):
            scanned += 1
            pairs = extract_ab_pairs(source_row.get("normalized_text"))
            if not pairs:
                continue
            source_rows_with_pair += 1
            for pair_index, pair in enumerate(pairs, start=1):
                output_row = build_output_row(run_id, source_row, pair_index, pair)
                output_rows.append(output_row)
                pending.append(output_row)
                if not args.dry_run and len(pending) >= 1000:
                    insert_rows(conn, args.output_table, pending)
                    pending.clear()

        if not args.dry_run:
            insert_rows(conn, args.output_table, pending)

        csv_path = run_dir / "catfood_ab_comparisons.csv"
        write_csv(csv_path, output_rows)
        summary = {
            "run_id": run_id,
            "source_table": args.source_table,
            "output_table": args.output_table,
            "min_source_choice_id": min_source_choice_id,
            "candidate_rows_scanned": scanned,
            "source_rows_with_pair": source_rows_with_pair,
            "pair_rows": len(output_rows),
            "dry_run": bool(args.dry_run),
            "csv": str(csv_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
