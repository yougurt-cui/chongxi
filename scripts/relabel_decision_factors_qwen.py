#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
relabel_decision_factors_qwen.py

用通义千问重打 catfood_decision_comment_labels 中三个决策标准：
- 功能适配度
- 产品顾虑
- 品牌信任

规则版（decision_comment_rules_v1）只按关键词命中打标，误报/漏报较多。
本脚本让 LLM 结合语义判断这三类标准，二级标签严格限定在既有体系内；
价格顾虑 与 decision_result 保留原规则结果不动。

旧标签快照保存在 decision_label_json.rules_snapshot 中，便于逐行审计回滚。
正式运行前还应对整表做一次数据库备份。打完的行 label_version 更新为
decision_comment_qwen_v2，重复执行时自动跳过已经完成的行。

用法：
    python scripts/relabel_decision_factors_qwen.py --limit 20 --dry-run   # 试跑
    python scripts/relabel_decision_factors_qwen.py                        # 全量
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "scripts"
for path in (BASE_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app_config import get_qwen_config  # noqa: E402
from decision_comment_labeler import (  # noqa: E402
    DECISION_FACTOR_RULES,
    connect_mysql,
    normalize_text,
    quote_ident,
    unique_keep_order,
)
import pymysql  # noqa: E402


TARGET_TABLE = "catfood_decision_comment_labels"
RULES_VERSION = "decision_comment_rules_v1"
QWEN_VERSION = "decision_comment_qwen_v2"
LEGACY_QWEN_VERSIONS = ("decision_comment_qwen_v1",)

# 交给 LLM 重判的三个一级标准；价格顾虑保留规则结果
LLM_FACTORS = ("功能适配度", "产品顾虑", "品牌信任")
LLM_TAXONOMY = {
    primary: list(DECISION_FACTOR_RULES[primary].keys()) for primary in LLM_FACTORS
}
FACTOR_ORDER = list(DECISION_FACTOR_RULES.keys())

MAX_COMMENT_CHARS = 1200
MAX_EVIDENCE_CHARS = 40
FALLBACK_ROW_SLEEP = 0.2


MIN_CONFIDENCE = 0.72


SYSTEM_PROMPT = """你是猫粮购买决策评论的严格证据分类器。每条评论必须独立判断，禁止利用同批其他评论的信息。
你的任务：判断用户在做购买决策时，是否真的在使用下面三个决策标准，并选出对应二级标签。

一级标准定义：
- 功能适配度：用户围绕猫的健康问题（肠胃、毛发、泌尿、体重、过敏等）、生理阶段（幼猫/成猫/老年猫/绝育）或品种个体差异，考量产品是否适合自家猫。
- 产品顾虑：用户对产品本身的属性表达担忧或把它作为取舍依据，如配方复杂度、肉源结构、营养指标、油脂、适口性、肠胃耐受、原料、工艺、稳定性、安全品控、颗粒气味形态。
- 品牌信任：用户把品牌层面的信任因素作为决策依据，如品牌口碑、他人评价、品控信任、自己历史使用经验、代工厂/生产方、原料来源透明度。

各标准允许的二级标签（禁止编造列表之外的标签）：
{taxonomy}

判定要求：
1. 只有评论原文提供明确证据，并且该因素确实参与选择、取舍、适用性判断或风险判断时才打标。拿不准就不打标。
2. 一条评论可以命中 0 到 3 个一级标准；每个命中的标准给出 1 个或多个二级标签。
3. evidence 必须逐字摘录当前评论原文，不能改写、概括或补全，不超过 20 字；找不到原文证据就不打标。
4. 不要判断价格因素，也不要判断购买结果。
5. 功能适配度必须同时存在猫的健康/阶段/个体条件，以及产品是否适合或实际效果的语义。只说“减肥成功了吗”“对比某品牌”不算。
6. 产品顾虑必须包含对产品属性的疑问、担忧、否定、风险或明确取舍。仅出现“烘焙、冻干、鲜肉、鸭肉、颗粒、原料、工厂、适口性”等中性属性词不算。
7. 品牌信任必须明确涉及口碑、真假、可靠性、品控、他人评价、本人使用经验、生产方或透明度。只问两个品牌哪个好、只出现品牌名或泛泛求推荐不算。
8. 一段文字可能是多人对话合集；只根据文字中明确表达的判断打标，不推断说话者身份和未写出的背景。

反例：
- “这是烘焙粮” → factors=[]（中性属性）
- “已经买了六包鸭肉” → factors=[]（口味/购买陈述）
- “A和B哪个好” → factors=[]（没有给出本任务三类标准）
- “减肥成功了吗” → factors=[]（缺少产品适配或效果证据）
正例：
- “我家猫尿闭过，这款吃了半年没复发” → 功能适配度/泌尿适配
- “担心蛋白太高伤肾，所以没买” → 产品顾虑/营养指标
- “这个牌子以前吃过三年，品控一直稳定” → 品牌信任/历史使用经验、品控信任

只返回一个合法 JSON 对象，不要 Markdown，结构：
{{"results":[{{"idx":0,"factors":[{{"primary":"功能适配度","secondary":["肠胃/消化适配"],"evidence":"原文片段","confidence":0.93}}]}}]}}
results 必须覆盖输入的每个 idx；没有命中任何标准时 factors 为空数组。"""


def build_system_prompt():
    taxonomy_lines = []
    for primary, secondaries in LLM_TAXONOMY.items():
        taxonomy_lines.append(f"- {primary}：{'、'.join(secondaries)}")
    return SYSTEM_PROMPT.format(taxonomy="\n".join(taxonomy_lines))


def call_qwen(cfg, system_prompt, user_content, *, timeout=120):
    resp = requests.post(
        f"{cfg['base_url']}/chat/completions",
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"] or ""


def extract_json(raw):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型未返回 JSON 对象")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型返回值不是 JSON 对象")
    return data


def validate_results(data, comments):
    """校验并清洗模型输出，返回 {idx: [factor, ...]}；标签严格限定在体系内。"""
    parsed = {}
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("results 字段缺失或不是数组")
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("idx"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(comments) or idx in parsed:
            continue
        factors = []
        seen_primary = set()
        for factor in item.get("factors") or []:
            if not isinstance(factor, dict):
                continue
            primary = normalize_text(factor.get("primary"))
            if primary not in LLM_TAXONOMY or primary in seen_primary:
                continue
            allowed = LLM_TAXONOMY[primary]
            secondary = unique_keep_order(
                [
                    normalize_text(sec)
                    for sec in factor.get("secondary") or []
                    if normalize_text(sec) in allowed
                ]
            )
            if not secondary:
                continue
            evidence = normalize_text(factor.get("evidence"))[:MAX_EVIDENCE_CHARS]
            try:
                confidence = float(factor.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            # 两道硬门槛：模型必须足够确定，证据必须能在本条原文中逐字找到。
            if confidence < MIN_CONFIDENCE or not evidence or evidence not in comments[idx]:
                continue
            seen_primary.add(primary)
            factors.append(
                {
                    "primary": primary,
                    "secondary": secondary,
                    "evidence": evidence,
                    "confidence": round(confidence, 4),
                }
            )
        parsed[idx] = factors
    missing = [i for i in range(len(comments)) if i not in parsed]
    if missing:
        raise ValueError(f"模型输出缺少 idx：{missing}")
    return parsed


def label_batch(cfg, system_prompt, comments, *, max_retries=3, retry_sleep=3):
    """批量打标；整批失败时自动降级为逐条重试，单条失败的行返回 None。"""
    user_content = json.dumps(
        {
            "comments": [
                {"idx": i, "text": text[:MAX_COMMENT_CHARS]}
                for i, text in enumerate(comments)
            ]
        },
        ensure_ascii=False,
    )
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            raw = call_qwen(cfg, system_prompt, user_content)
            return validate_results(extract_json(raw), comments)
        except (requests.RequestException, ValueError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
            print(f"  批次调用失败（第 {attempt}/{max_retries} 次）：{exc}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(retry_sleep * attempt)
    # 降级：逐条重试，避免一条异常拖垮整批；单条失败的行记为 None
    print(f"  整批失败，降级为逐条重试（{len(comments)} 条）：{last_error}", file=sys.stderr)
    fallback = {}
    for idx, text in enumerate(comments):
        single_content = json.dumps(
            {"comments": [{"idx": 0, "text": text[:MAX_COMMENT_CHARS]}]},
            ensure_ascii=False,
        )
        for attempt in range(1, max_retries + 1):
            try:
                raw = call_qwen(cfg, system_prompt, single_content)
                fallback[idx] = validate_results(extract_json(raw), [text])[0]
                break
            except (
                requests.RequestException, ValueError, json.JSONDecodeError, KeyError
            ) as exc:
                print(f"  第 {idx} 条单独调用失败（{attempt}/{max_retries}）：{exc}", file=sys.stderr)
                if attempt < max_retries:
                    time.sleep(retry_sleep * attempt)
        else:
            fallback[idx] = None
        if FALLBACK_ROW_SLEEP > 0:
            time.sleep(FALLBACK_ROW_SLEEP)
    return fallback


def split_labels(value):
    return [item.strip() for item in str(value or "").split("|") if item.strip()]


def merge_row(row, llm_factors, model_name):
    """LLM 重判三类标准，价格顾虑与结果保留规则版；返回更新字段。"""
    old_primary = split_labels(row.get("decision_factor_primary"))
    old_secondary = split_labels(row.get("decision_factor_secondary"))
    old_keywords = split_labels(row.get("decision_matched_keywords"))

    kept_primary = [p for p in old_primary if p not in LLM_FACTORS]
    kept_secondary = [
        s for s in old_secondary if s.split(">", 1)[0] not in LLM_FACTORS
    ]
    kept_keywords = [
        k
        for k in old_keywords
        if not (
            k.startswith("标准:")
            and k.split(":", 2)[1].split(">", 1)[0] in LLM_FACTORS
        )
    ]

    llm_primary = [f["primary"] for f in llm_factors]
    llm_secondary = []
    llm_keywords = []
    for factor in llm_factors:
        for sec in factor["secondary"]:
            llm_secondary.append(f"{factor['primary']}>{sec}")
        if factor.get("evidence"):
            llm_keywords.append(
                f"千问:{factor['primary']}:{factor['evidence']}"
            )

    primary = [
        p
        for p in FACTOR_ORDER
        if p in set(kept_primary) | set(llm_primary)
    ]
    ordered_secondary = unique_keep_order(
        [s for s in llm_secondary + kept_secondary]
    )
    ordered_secondary.sort(key=lambda s: FACTOR_ORDER.index(s.split(">", 1)[0]))
    keywords = unique_keep_order(llm_keywords + kept_keywords)

    try:
        label_json = json.loads(row.get("decision_label_json") or "{}")
        if not isinstance(label_json, dict):
            label_json = {}
    except (TypeError, ValueError):
        label_json = {}
    if "rules_snapshot" not in label_json:
        label_json["rules_snapshot"] = {
            "decision_factor_primary": old_primary,
            "decision_factor_secondary": old_secondary,
            "matched_keywords": old_keywords,
            "label_version": row.get("label_version"),
        }
    else:
        # 二次重打（v1→v2 升级）时，把上一轮结果归档到历史轨迹
        label_json.setdefault("label_history", []).append({
            "label_version": row.get("label_version"),
            "decision_factor_primary": old_primary,
            "decision_factor_secondary": old_secondary,
            "llm": label_json.get("llm"),
        })
    label_json["decision_factor_primary"] = primary
    label_json["decision_factor_secondary"] = ordered_secondary
    label_json["matched_keywords"] = keywords
    label_json["llm"] = {
        "model": model_name,
        "factors": llm_factors,
        "relabeled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    return {
        "id": row["id"],
        "decision_factor_primary": " | ".join(primary),
        "decision_factor_secondary": " | ".join(ordered_secondary),
        "decision_matched_keywords": " | ".join(keywords),
        "decision_label_json": json.dumps(label_json, ensure_ascii=False),
        "label_version": QWEN_VERSION,
        "labeled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def fetch_pending_rows(
    conn, *, limit=0, ids=None, versions=(RULES_VERSION,), id_min=None, id_max=None
):
    sql = f"""
        SELECT id, comment_text, decision_factor_primary, decision_factor_secondary,
               decision_matched_keywords, decision_label_json, label_version
        FROM {quote_ident(TARGET_TABLE)}
        WHERE label_version IN ({','.join(['%s'] * len(versions))})
          AND comment_text IS NOT NULL AND TRIM(comment_text) <> ''
    """
    params = list(versions)
    if ids:
        placeholders = ",".join(["%s"] * len(ids))
        sql += f" AND id IN ({placeholders})"
        params.extend(ids)
    if id_min is not None:
        sql += " AND id >= %s"
        params.append(int(id_min))
    if id_max is not None:
        sql += " AND id <= %s"
        params.append(int(id_max))
    sql += " ORDER BY id ASC"
    if limit > 0:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def update_rows(conn, updates):
    if not updates:
        return 0
    sql = f"""
        UPDATE {quote_ident(TARGET_TABLE)}
        SET decision_factor_primary=%(decision_factor_primary)s,
            decision_factor_secondary=%(decision_factor_secondary)s,
            decision_matched_keywords=%(decision_matched_keywords)s,
            decision_label_json=%(decision_label_json)s,
            label_version=%(label_version)s,
            labeled_at=%(labeled_at)s
        WHERE id=%(id)s
    """
    with conn.cursor() as cur:
        cur.executemany(sql, updates)
    conn.commit()
    return len(updates)


def run(args):
    cfg = get_qwen_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置通义千问 API Key，请设置 DASHSCOPE_API_KEY 或 QWEN_API_KEY")
    if args.model:
        cfg["model"] = args.model
    system_prompt = build_system_prompt()
    versions = (RULES_VERSION,)
    if args.relabel_existing:
        versions = (RULES_VERSION, QWEN_VERSION) + LEGACY_QWEN_VERSIONS

    conn = connect_mysql(cursorclass=pymysql.cursors.DictCursor)
    processed = updated = skipped = changed = 0
    factor_counts = {p: 0 for p in LLM_FACTORS}
    try:
        rows = fetch_pending_rows(
            conn, limit=args.limit, ids=args.ids, versions=versions,
            id_min=args.id_min, id_max=args.id_max,
        )
        total = len(rows)
        print(f"待重打行数：{total}（batch={args.batch_size}, model={cfg['model']}）")

        for start in range(0, total, args.batch_size):
            batch = rows[start : start + args.batch_size]
            comments = [normalize_text(r["comment_text"]) for r in batch]
            results = label_batch(
                cfg, system_prompt, comments,
                max_retries=args.max_retries, retry_sleep=args.retry_sleep,
            )

            updates = []
            for i, row in enumerate(batch):
                llm_factors = results.get(i)
                if llm_factors is None:
                    skipped += 1
                    continue
                update = merge_row(row, llm_factors, cfg["model"])
                processed += 1
                for factor in llm_factors:
                    factor_counts[factor["primary"]] += 1
                old = split_labels(row.get("decision_factor_primary"))
                new = split_labels(update["decision_factor_primary"])
                if old != new:
                    changed += 1
                if args.dry_run:
                    print(
                        json.dumps(
                            {
                                "id": row["id"],
                                "comment": normalize_text(row["comment_text"])[:80],
                                "old_primary": old,
                                "new_primary": new,
                                "new_secondary": split_labels(
                                    update["decision_factor_secondary"]
                                ),
                                "llm_factors": llm_factors,
                            },
                            ensure_ascii=False,
                        )
                    )
                else:
                    updates.append(update)

            if not args.dry_run:
                updated += update_rows(conn, updates)
            done = min(start + args.batch_size, total)
            print(f"进度：{done}/{total}（已更新 {updated}，标签变化 {changed}）")
            if args.sleep > 0:
                time.sleep(args.sleep)
    finally:
        conn.close()

    summary = {
        "mode": "qwen_relabel",
        "target_table": TARGET_TABLE,
        "label_version": QWEN_VERSION,
        "model": cfg["model"],
        "dry_run": args.dry_run,
        "processed": processed,
        "updated": updated,
        "primary_changed": changed,
        "skipped_rows": skipped,
        "llm_factor_counts": factor_counts,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="通义千问重打 Decision 评论的功能适配度/产品顾虑/品牌信任标签"
    )
    parser.add_argument("--limit", type=int, default=0, help="最多处理行数，0 表示全部")
    parser.add_argument("--ids", type=int, nargs="*", default=None, help="只处理指定行 id")
    parser.add_argument("--id-min", type=int, default=None, help="仅处理 id 大于等于此值的行")
    parser.add_argument("--id-max", type=int, default=None, help="仅处理 id 小于等于此值的行")
    parser.add_argument("--batch-size", type=int, default=20, help="每次请求携带的评论条数")
    parser.add_argument("--model", default=None, help="覆盖默认模型，如 qwen-plus / qwen-max")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=3.0)
    parser.add_argument("--sleep", type=float, default=0.0, help="批次之间的休眠秒数")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写库")
    parser.add_argument(
        "--relabel-existing",
        action="store_true",
        help="同时重打已经标为 decision_comment_qwen_v* 的行（用于版本升级）"
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size 至少为 1")
    run(args)


if __name__ == "__main__":
    main()
