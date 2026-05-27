#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pymysql
from openai import OpenAI


QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")

DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", os.getenv("DB_HOST", "127.0.0.1")),
    "port": int(os.getenv("MYSQL_PORT", os.getenv("DB_PORT", "3306"))),
    "user": os.getenv("MYSQL_USER", os.getenv("DB_USER", "root")),
    "password": os.getenv("MYSQL_PASSWORD", os.getenv("DB_PASSWORD", "")),
    "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
}

SOURCE_DB = os.getenv("PROCESS_SIGNAL_SOURCE_DB", "csv_labeling")
SOURCE_TABLE = os.getenv("PROCESS_SIGNAL_SOURCE_TABLE", "catfood_process_signal_candidates")
OUTPUT_DB = os.getenv("PROCESS_SIGNAL_OUTPUT_DB", "csv_labeling")
OUTPUT_TABLE = os.getenv("PROCESS_SIGNAL_STANDARD_TABLE", "catfood_brand_process_signal_standardized")

CONFIDENCE_LEVELS = {"高", "中", "低"}
REVIEW_STATUSES = {"待复核", "通过", "驳回"}
SIGNAL_POLARITIES = {"负向", "正向"}


TAXONOMY: dict[str, dict[str, dict[str, Any]]] = {
    "批次变化": {
        "复购前后差异": {
            "expressions": ["以前吃没事", "之前吃没问题", "这次买的有问题", "以前不拉这次拉", "这次不一样"],
            "qc_items": ["批次留样对比", "水分", "酸价", "过氧化值", "颗粒硬度", "粉化率"],
        },
        "批次稳定性异常": {
            "expressions": ["这批", "这次", "这一袋", "这一箱", "新批次", "批次不稳定"],
            "qc_items": ["批次留样对比", "水分", "酸价", "过氧化值", "颗粒硬度", "粉化率"],
        },
        "包装/版本变更可疑": {
            "expressions": ["换包装后", "新版", "升级后", "改版后"],
            "qc_items": ["批次留样对比", "水分", "酸价", "过氧化值", "颗粒硬度", "粉化率"],
        },
        "感官批次差异": {
            "expressions": ["颜色变了", "味道变了", "颗粒变了", "比以前油", "比以前硬"],
            "qc_items": ["批次留样对比", "水分", "酸价", "过氧化值", "颗粒硬度", "粉化率"],
        },
    },
    "表面油脂": {
        "表面油脂残留可疑": {
            "expressions": ["颗粒很油", "粮很油", "摸起来油", "油乎乎", "油腻", "表面有油"],
            "qc_items": ["表面油脂残留", "后喷涂比例", "喷涂均匀度", "酸价", "过氧化值"],
        },
        "后喷涂控制可疑": {
            "expressions": ["碗底有油", "袋子里有油", "颗粒发亮", "亮晶晶", "有些颗粒特别油"],
            "qc_items": ["表面油脂残留", "后喷涂比例", "喷涂均匀度", "酸价", "过氧化值"],
        },
        "接触性油脂风险": {
            "expressions": ["吃完下巴油", "下巴油油的", "嘴边油", "下巴毛油", "嘴巴一圈油"],
            "qc_items": ["表面油脂残留", "后喷涂比例", "喷涂均匀度", "酸价", "过氧化值"],
        },
        "批次油脂波动可疑": {
            "expressions": ["这批更油", "这次特别油", "比以前油很多"],
            "qc_items": ["表面油脂残留", "后喷涂比例", "喷涂均匀度", "酸价", "过氧化值"],
        },
    },
    "气味氧化": {
        "油脂氧化风险": {
            "expressions": ["哈喇味", "油耗味", "油味怪", "油味重"],
            "qc_items": ["酸价", "过氧化值", "水分", "水活度", "包装密封性", "挥发性气味指标"],
        },
        "气味刺激可疑": {
            "expressions": ["味道很冲", "味道刺鼻", "香精味重", "诱食剂味", "闻着难受"],
            "qc_items": ["酸价", "过氧化值", "水分", "水活度", "包装密封性", "挥发性气味指标"],
        },
        "新鲜度异常": {
            "expressions": ["不新鲜", "变质味", "酸味", "怪味"],
            "qc_items": ["酸价", "过氧化值", "水分", "水活度", "包装密封性", "挥发性气味指标"],
        },
        "仓储稳定性可疑": {
            "expressions": ["霉味", "受潮味", "闷味"],
            "qc_items": ["酸价", "过氧化值", "水分", "水活度", "包装密封性", "挥发性气味指标"],
        },
    },
    "颗粒物性": {
        "颗粒硬度异常": {
            "expressions": ["颗粒太硬", "很硬", "咬不动", "猫不嚼"],
            "qc_items": ["颗粒硬度", "颗粒尺寸分布", "颗粒均匀度", "膨化密度", "水分"],
        },
        "颗粒尺寸不适配": {
            "expressions": ["颗粒太大", "颗粒太小", "卡嗓子", "噎住"],
            "qc_items": ["颗粒硬度", "颗粒尺寸分布", "颗粒均匀度", "膨化密度", "水分"],
        },
        "颗粒均匀度异常": {
            "expressions": ["颗粒不均匀", "大小不一", "有大有小"],
            "qc_items": ["颗粒硬度", "颗粒尺寸分布", "颗粒均匀度", "膨化密度", "水分"],
        },
        "消化适配可疑": {
            "expressions": ["吐整粒", "吐出来还是完整颗粒", "便便里有整粒", "拉出来还有粮", "直接吞"],
            "qc_items": ["颗粒硬度", "颗粒尺寸分布", "颗粒均匀度", "膨化密度", "水分"],
        },
    },
    "粉化碎渣": {
        "粉化率偏高": {
            "expressions": ["粉很多", "粉末多", "袋底全是粉", "底下都是粉"],
            "qc_items": ["粉化率", "颗粒强度", "运输破碎率", "包装抗压性"],
        },
        "颗粒强度不足": {
            "expressions": ["一捏就碎", "容易碎", "碎渣多", "掉渣"],
            "qc_items": ["粉化率", "颗粒强度", "运输破碎率", "包装抗压性"],
        },
        "运输破碎可疑": {
            "expressions": ["到手碎了", "打开全是渣", "运输后碎很多"],
            "qc_items": ["粉化率", "颗粒强度", "运输破碎率", "包装抗压性"],
        },
        "粉尘刺激可疑": {
            "expressions": ["粉尘大", "猫吃的时候呛", "吃的时候咳"],
            "qc_items": ["粉化率", "颗粒强度", "运输破碎率", "包装抗压性"],
        },
    },
    "水分受潮": {
        "水分控制异常": {
            "expressions": ["粮软", "不脆", "潮潮的", "有点潮"],
            "qc_items": ["水分", "水活度", "包装密封性", "微生物指标", "霉菌毒素"],
        },
        "包装密封可疑": {
            "expressions": ["受潮", "袋子漏气", "封口不好"],
            "qc_items": ["水分", "水活度", "包装密封性", "微生物指标", "霉菌毒素"],
        },
        "霉变风险": {
            "expressions": ["发霉", "霉味", "长毛"],
            "qc_items": ["水分", "水活度", "包装密封性", "微生物指标", "霉菌毒素"],
        },
        "结块异常": {
            "expressions": ["结块", "黏在一起", "一坨一坨"],
            "qc_items": ["水分", "水活度", "包装密封性", "微生物指标", "霉菌毒素"],
        },
    },
    "熟化消化": {
        "熟化度不足可疑": {
            "expressions": ["不消化", "便便里有颗粒", "没消化完"],
            "qc_items": ["淀粉糊化度", "蛋白消化率", "颗粒硬度", "膨化温度", "膨化压力", "水分"],
        },
        "淀粉糊化不足可疑": {
            "expressions": ["拉出来有粮", "吃进去什么样出来什么样"],
            "qc_items": ["淀粉糊化度", "蛋白消化率", "颗粒硬度", "膨化温度", "膨化压力", "水分"],
        },
        "蛋白消化率波动可疑": {
            "expressions": ["便臭", "胀气", "软便明显"],
            "qc_items": ["淀粉糊化度", "蛋白消化率", "颗粒硬度", "膨化温度", "膨化压力", "水分"],
        },
        "胃肠刺激可疑": {
            "expressions": ["吃完马上吐", "吃完马上拉", "吃了就吐", "吃了就拉"],
            "qc_items": ["淀粉糊化度", "蛋白消化率", "颗粒硬度", "膨化温度", "膨化压力", "水分"],
        },
    },
}


@dataclass(frozen=True)
class StandardTag:
    level_1: str
    level_2: str
    qc_items: list[str]


ALLOWED_TAGS = {
    (level_1, level_2): StandardTag(level_1, level_2, list(meta["qc_items"]))
    for level_1, tags in TAXONOMY.items()
    for level_2, meta in tags.items()
}


SYSTEM_PROMPT = """
你是宠物食品B端工艺线索标准化标注助手。

你的任务：
1. 输入是规则初筛后的猫粮评论候选。
2. 判断评论是否真的包含明确的产品状态/批次/工艺线索。
3. 如果包含，将其标准化为指定的一级工艺线索和二级标准标签。
4. 同时抽取品牌信息，因为输出表是品牌工艺信号线索表。

强约束：
1. 只能基于原文证据标注，不要根据病症结果反推工艺。
2. 黑下巴、软便、拉稀、呕吐、便秘、尿闭、泪痕本身不是工艺线索。
3. 只说“不爱吃”“吃了不舒服”“担心有问题”，没有产品状态/批次表达时，不标。
4. 同一条候选默认保留1个最明确标签，最多2个标签。
5. 一级和二级标签必须从给定标签体系中选择，不得创造新标签。
6. 如果候选表原有标签不符合标准体系，必须重新判断，不能照抄。
7. 品牌只抽原文、标题、正文或搜索词明确出现的品牌；不确定则 brand_name 为空。
8. 不要把品类词、功效词、病症词当品牌，例如“猫粮”“处方粮”“肠胃粮”“泌尿粮”不是品牌。
9. 只输出合法JSON，不输出Markdown，不输出解释过程。
"""


def build_user_prompt(rows: list[dict[str, Any]]) -> str:
    taxonomy_payload = {
        level_1: {
            level_2: {
                "typical_expressions": meta["expressions"],
                "qc_items": meta["qc_items"],
            }
            for level_2, meta in tags.items()
        }
        for level_1, tags in TAXONOMY.items()
    }
    compact_rows = []
    for row in rows:
        compact_rows.append(
            {
                "candidate_id": row["id"],
                "source_platform": row.get("source_platform"),
                "source_keyword": row.get("source_keyword"),
                "source_title": row.get("source_title"),
                "source_content": truncate_text(row.get("source_content"), 500),
                "comment_text": truncate_text(row.get("comment_text"), 900),
                "rule_level_1": row.get("process_signal_category"),
                "rule_level_2": row.get("process_signal_tag"),
                "rule_expression": row.get("matched_expression"),
                "rule_evidence": row.get("evidence_text"),
                "rule_confidence": row.get("confidence_level"),
            }
        )

    return f"""
请对下面候选评论进行工艺线索标准化标注，并抽取品牌信息。

允许的标签体系如下：
{json.dumps(taxonomy_payload, ensure_ascii=False, indent=2)}

请严格返回如下JSON结构：
{{
  "items": [
    {{
      "candidate_id": 123,
      "brand_name": "",
      "brand_mentions": [],
      "brand_evidence_text": "",
      "brand_confidence": "高/中/低",
      "has_process_signal": true,
      "labels": [
        {{
          "process_level_1": "",
          "process_level_2": "",
          "process_evidence_text": "",
          "matched_expression": "",
          "signal_polarity": "负向/正向",
          "process_confidence": "高/中/低",
          "recommended_qc_items": [],
          "review_status": "待复核"
        }}
      ],
      "reject_reason": ""
    }}
  ]
}}

字段规则：
- 每个输入 candidate_id 必须返回一个 item。
- has_process_signal=false 时 labels 必须为空，并填写 reject_reason。
- process_level_1 / process_level_2 必须来自允许标签体系。
- recommended_qc_items 优先使用对应二级标签的建议QC项，最多6个。
- process_evidence_text 必须是原文短证据，最长80个中文字符。
- matched_expression 必须是原文里的触发表达或同义短语，最长20个中文字符。
- signal_polarity 只能是“负向”或“正向”。
- 负向：用户描述产品状态异常、可疑、变差、不适配、批次波动。
- 正向：用户明确描述产品状态稳定、改善、没有异常、颗粒/气味/油脂状态是好的。
- review_status 固定填“待复核”。
- brand_mentions 最多3个。
- brand_confidence 和 process_confidence 只能是“高”“中”“低”。

候选评论如下：
{json.dumps(compact_rows, ensure_ascii=False, indent=2)}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Use Qwen to standardize catfood process signal candidates into brand process signal tags.")
    parser.add_argument("--source-db", default=SOURCE_DB)
    parser.add_argument("--source-table", default=SOURCE_TABLE)
    parser.add_argument("--output-db", default=OUTPUT_DB)
    parser.add_argument("--output-table", default=OUTPUT_TABLE)
    parser.add_argument("--if-exists", choices=("replace", "append", "fail"), default="replace")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0, help="0 means all rows")
    parser.add_argument("--where", default="", help="optional SQL condition without WHERE")
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--max-comment-chars", type=int, default=500, help="skip rows whose comment_text is longer than this")
    parser.add_argument("--skip-multiline", action=argparse.BooleanOptionalAction, default=True, help="skip rows whose comment_text contains newline")
    return parser.parse_args()


def get_qwen_client() -> OpenAI:
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("未检测到 DASHSCOPE_API_KEY。请先设置环境变量。")
    return OpenAI(api_key=api_key, base_url=QWEN_BASE_URL)


def connect_mysql(cursorclass=pymysql.cursors.DictCursor):
    return pymysql.connect(**DB_CONFIG, cursorclass=cursorclass)


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"Unsafe identifier: {name}")
    return f"`{name}`"


def truncate_text(value: Any, max_len: int) -> str:
    text = "" if value is None else str(value).strip()
    return text if len(text) <= max_len else text[:max_len]


def normalize_list(value: Any, max_items: int = 6) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result[:max_items]


def safe_parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise


def create_output_table(conn, output_db: str, output_table: str, if_exists: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {quote_ident(output_db)} DEFAULT CHARACTER SET utf8mb4")
        cur.execute(
            """
            SELECT COUNT(*)
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """,
            (output_db, output_table),
        )
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
              source_candidate_id BIGINT NOT NULL,
              source_platform VARCHAR(32) NOT NULL,
              source_row_id BIGINT NULL,
              external_id VARCHAR(255) NULL,
              source_keyword VARCHAR(255) NULL,
              source_comment_time VARCHAR(64) NULL,
              brand_name VARCHAR(128) NULL,
              brand_mentions VARCHAR(255) NULL,
              brand_confidence VARCHAR(16) NOT NULL,
              process_level_1 VARCHAR(64) NOT NULL,
              process_level_2 VARCHAR(128) NOT NULL,
              process_evidence_text VARCHAR(300) NOT NULL,
              matched_expression VARCHAR(128) NOT NULL,
              signal_polarity VARCHAR(16) NOT NULL,
              process_confidence VARCHAR(16) NOT NULL,
              recommended_qc_items VARCHAR(500) NOT NULL,
              review_status VARCHAR(32) NOT NULL,
              model_name VARCHAR(64) NOT NULL,
              comment_text VARCHAR(800) NOT NULL,
              source_title TEXT NULL,
              created_at DATETIME NOT NULL,
              KEY idx_candidate (source_candidate_id),
              KEY idx_brand (brand_name),
              KEY idx_process_tag (process_level_1, process_level_2),
              KEY idx_review (review_status),
              KEY idx_source (source_platform, source_row_id)
            ) DEFAULT CHARSET=utf8mb4
            """
        )
    conn.commit()


def fetch_source_rows(conn, source_db: str, source_table: str, where: str, limit: int) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {quote_ident(source_db)}.{quote_ident(source_table)}"
    if where.strip():
        sql += f" WHERE {where.strip()}"
    sql += " ORDER BY id"
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def chunks(rows: list[dict[str, Any]], size: int):
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def is_model_input_row(row: dict[str, Any], max_comment_chars: int, skip_multiline: bool) -> bool:
    comment_text = "" if row.get("comment_text") is None else str(row.get("comment_text"))
    if not comment_text.strip():
        return False
    if skip_multiline and ("\n" in comment_text or "\r" in comment_text):
        return False
    if max_comment_chars > 0 and len(comment_text.strip()) > max_comment_chars:
        return False
    return True


def dedupe_key(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("comment_text") or ""),
        str(row.get("evidence_text") or ""),
        str(row.get("matched_expression") or ""),
    ]
    normalized = " ".join(parts)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def dedupe_source_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        key = dedupe_key(row)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        result.append(row)
    return result, skipped


def call_qwen(client: OpenAI, rows: list[dict[str, Any]], model: str, temperature: float) -> tuple[dict[str, Any], str]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(rows)},
    ]
    response = client.chat.completions.create(model=model, messages=messages, temperature=temperature)
    content = response.choices[0].message.content or ""
    return safe_parse_json(content), content


def normalize_item(raw_item: dict[str, Any], source_row: dict[str, Any]) -> dict[str, Any]:
    brand_confidence = str(raw_item.get("brand_confidence") or "低").strip()
    if brand_confidence not in CONFIDENCE_LEVELS:
        brand_confidence = "低"

    has_process_signal = bool(raw_item.get("has_process_signal"))
    labels = raw_item.get("labels") if isinstance(raw_item.get("labels"), list) else []
    normalized_labels = []
    for label in labels[:2]:
        if not isinstance(label, dict):
            continue
        level_1 = str(label.get("process_level_1") or "").strip()
        level_2 = str(label.get("process_level_2") or "").strip()
        standard = ALLOWED_TAGS.get((level_1, level_2))
        if standard is None:
            continue
        confidence = str(label.get("process_confidence") or source_row.get("confidence_level") or "低").strip()
        if confidence not in CONFIDENCE_LEVELS:
            confidence = "低"
        polarity = str(label.get("signal_polarity") or "").strip()
        if polarity not in SIGNAL_POLARITIES:
            polarity = "负向"
        review_status = str(label.get("review_status") or "待复核").strip()
        if review_status not in REVIEW_STATUSES:
            review_status = "待复核"
        qc_items = normalize_list(label.get("recommended_qc_items"), 6) or standard.qc_items
        normalized_labels.append(
            {
                "process_level_1": level_1,
                "process_level_2": level_2,
                "process_evidence_text": truncate_text(label.get("process_evidence_text"), 120),
                "matched_expression": truncate_text(label.get("matched_expression"), 40),
                "signal_polarity": polarity,
                "process_confidence": confidence,
                "recommended_qc_items": qc_items[:6],
                "review_status": review_status,
            }
        )

    if not normalized_labels:
        has_process_signal = False

    return {
        "candidate_id": source_row["id"],
        "brand_name": truncate_text(raw_item.get("brand_name"), 128),
        "brand_mentions": normalize_list(raw_item.get("brand_mentions"), 3),
        "brand_evidence_text": truncate_text(raw_item.get("brand_evidence_text"), 160),
        "brand_confidence": brand_confidence,
        "has_process_signal": has_process_signal,
        "labels": normalized_labels,
        "reject_reason": truncate_text(raw_item.get("reject_reason"), 160),
    }


def fallback_item(source_row: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "candidate_id": source_row["id"],
        "brand_name": "",
        "brand_mentions": [],
        "brand_evidence_text": "",
        "brand_confidence": "低",
        "has_process_signal": False,
        "labels": [],
        "reject_reason": truncate_text(f"模型调用失败：{error}", 160),
    }


def build_insert_rows(
    run_id: str,
    model: str,
    source_by_id: dict[int, dict[str, Any]],
    normalized_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for item in normalized_items:
        if not item["has_process_signal"]:
            continue
        source_row = source_by_id[int(item["candidate_id"])]
        base = {
            "run_id": run_id,
            "source_candidate_id": source_row["id"],
            "source_platform": source_row.get("source_platform"),
            "source_row_id": source_row.get("source_row_id"),
            "external_id": source_row.get("external_id"),
            "source_keyword": source_row.get("source_keyword"),
            "source_comment_time": source_row.get("source_comment_time"),
            "brand_name": item["brand_name"] or None,
            "brand_mentions": "、".join(item["brand_mentions"]) or None,
            "brand_confidence": item["brand_confidence"],
            "model_name": model,
            "comment_text": truncate_text(source_row.get("comment_text"), 800),
            "source_title": source_row.get("source_title"),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        for label in item["labels"]:
            rows.append(
                {
                    **base,
                    "process_level_1": label["process_level_1"],
                    "process_level_2": label["process_level_2"],
                    "process_evidence_text": label["process_evidence_text"] or label["matched_expression"],
                    "matched_expression": label["matched_expression"] or label["process_level_2"],
                    "signal_polarity": label["signal_polarity"],
                    "process_confidence": label["process_confidence"],
                    "recommended_qc_items": "、".join(label["recommended_qc_items"]) or "",
                    "review_status": label["review_status"],
                }
            )
    return rows


def insert_rows(conn, output_db: str, output_table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = f"""
        INSERT INTO {quote_ident(output_db)}.{quote_ident(output_table)} (
          run_id, source_candidate_id, source_platform, source_row_id,
          external_id, source_keyword, source_comment_time, brand_name, brand_mentions,
          brand_confidence, process_level_1, process_level_2,
          process_evidence_text, matched_expression, signal_polarity, process_confidence, recommended_qc_items,
          review_status, model_name, comment_text, source_title, created_at
        )
        VALUES (
          %(run_id)s, %(source_candidate_id)s, %(source_platform)s, %(source_row_id)s,
          %(external_id)s, %(source_keyword)s, %(source_comment_time)s, %(brand_name)s, %(brand_mentions)s,
          %(brand_confidence)s, %(process_level_1)s, %(process_level_2)s,
          %(process_evidence_text)s, %(matched_expression)s, %(signal_polarity)s, %(process_confidence)s, %(recommended_qc_items)s,
          %(review_status)s, %(model_name)s, %(comment_text)s, %(source_title)s, %(created_at)s
        )
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()


def standardize(args: argparse.Namespace) -> None:
    run_id = "brand_process_signal_std_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    client = get_qwen_client()
    conn = connect_mysql()
    try:
        create_output_table(conn, args.output_db, args.output_table, args.if_exists)
        all_source_rows = fetch_source_rows(conn, args.source_db, args.source_table, args.where, args.limit)
        filtered_rows = [
            row for row in all_source_rows
            if is_model_input_row(row, args.max_comment_chars, args.skip_multiline)
        ]
        source_rows, duplicate_skipped = dedupe_source_rows(filtered_rows)
        total = len(source_rows)
        skipped = len(all_source_rows) - len(filtered_rows)
        inserted = 0
        confirmed = 0
        rejected = 0
        print(f"run_id={run_id}")
        print(f"source={args.source_db}.{args.source_table}")
        print(f"output={args.output_db}.{args.output_table}")
        print(f"rows={total}")
        print(f"skipped_before_model={skipped}")
        print(f"skipped_duplicates={duplicate_skipped}")

        for batch_index, batch in enumerate(chunks(source_rows, max(1, args.batch_size)), start=1):
            source_by_id = {int(row["id"]): row for row in batch}
            print(f"[batch {batch_index}] rows={len(batch)} ids={min(source_by_id)}-{max(source_by_id)}", flush=True)
            try:
                payload, _raw_text = call_qwen(client, batch, args.model, args.temperature)
                raw_items = payload.get("items") if isinstance(payload, dict) else None
                if not isinstance(raw_items, list):
                    raise ValueError("模型输出缺少 items 数组")
                raw_by_id = {int(item.get("candidate_id")): item for item in raw_items if isinstance(item, dict) and item.get("candidate_id") in source_by_id}
                normalized_items = [
                    normalize_item(raw_by_id[row_id], source_by_id[row_id])
                    if row_id in raw_by_id
                    else fallback_item(source_by_id[row_id], "模型未返回该 candidate_id")
                    for row_id in source_by_id
                ]
            except Exception as exc:
                payload = {"error": str(exc)}
                normalized_items = [fallback_item(row, str(exc)) for row in batch]

            insert_payload = build_insert_rows(run_id, args.model, source_by_id, normalized_items)
            insert_rows(conn, args.output_db, args.output_table, insert_payload)
            inserted += len(insert_payload)
            confirmed += len(insert_payload)
            rejected += sum(1 for item in normalized_items if not item["has_process_signal"])
            print(f"[batch {batch_index}] inserted={len(insert_payload)} confirmed_total={confirmed} rejected_total={rejected}", flush=True)
            if args.sleep > 0:
                time.sleep(args.sleep)

        print(f"inserted={inserted}")
        print(f"confirmed={confirmed}")
        print(f"rejected={rejected}")
        print(f"skipped_before_model={skipped}")
        print(f"skipped_duplicates={duplicate_skipped}")
    finally:
        conn.close()


def main() -> int:
    standardize(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
