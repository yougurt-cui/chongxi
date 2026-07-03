#!/usr/bin/env python3
"""Match unresolved formula ingredients to standard ingredients with guarded LLM review."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import pymysql
from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = BASE_DIR / "vendor" / "feature_score_pipeline" / "scripts"
for path in (BASE_DIR, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app_config import get_mysql_config, get_qwen_config  # noqa: E402
from vendor.feature_score_pipeline.scripts.rebuild_protein_source_aggregate import (  # noqa: E402
    _normalize_ingredient_key,
    _split_grouped_alias_names,
)


SYSTEM_PROMPT = """你是宠物食品原材料标准化审核器。
每个 raw_name 只能在其 candidates 中选择，禁止编造 standard_ingredient_id。
要判断名称是否只是加工形态、繁简体、俗称、来源说明、含量说明或同义表达差异。
不要把不同动物种属、不同油脂、不同矿物质或不同功能添加剂强行合并。
返回 JSON 对象：{"results":[{"candidate_id":整数,"decision":"match_existing|need_new_standard|uncertain|discard_noise|discard_non_ingredient|discard_compound","standard_ingredient_id":字符串或null,"confidence":0到1,"reason":简短中文}]}。
只有语义明确等价时使用 match_existing；没有合适候选时使用 need_new_standard；无法确定时使用 uncertain。
OCR乱码、计量残片、标题等使用 discard_noise；不是配料的描述使用 discard_non_ingredient；包含多个不可拆分原料的复合文本使用 discard_compound。这三类不得建议新增标准原料。
"""


def _connect():
    return pymysql.connect(
        **get_mysql_config(), cursorclass=pymysql.cursors.DictCursor, autocommit=False
    )


def _extract_json(value: str) -> dict[str, Any]:
    text = (value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("模型未返回 JSON 对象")
    result = json.loads(text[start : end + 1])
    if not isinstance(result, dict):
        raise ValueError("模型返回值不是 JSON 对象")
    return result


def _similarity(left: str, right: str) -> float:
    a, b = _normalize_ingredient_key(left), _normalize_ingredient_key(right)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    containment = min(len(a), len(b)) / max(len(a), len(b)) if a in b or b in a else 0.0
    chars_a, chars_b = set(a), set(b)
    jaccard = len(chars_a & chars_b) / len(chars_a | chars_b)
    return round(max(ratio, containment, jaccard), 5)


def _valid_discard(raw_name: str, decision: str) -> bool:
    text = str(raw_name or "").strip()
    if decision == "discard_compound":
        list_markers = len(re.findall(r"[、,，;；]", text))
        generic_mix = any(word in text for word in ("复配", "复合", "矿物质", "维生素预混", "水果(", "蔬菜("))
        return list_markers >= 1 or generic_mix
    if decision == "discard_noise":
        return bool(re.search(r"有关此配方|卫生规定|产品分析|^[*#]+|^[A-Z ]+$", text, re.I))
    return True


def _load_master(cursor) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    cursor.execute("SELECT * FROM catfood_standard_ingredient WHERE active=1")
    standards = {row["standard_ingredient_id"]: dict(row) for row in cursor.fetchall()}
    cursor.execute("SELECT standard_ingredient_id,alias_names FROM catfood_standard_ingredient_alias")
    aliases: dict[str, list[str]] = defaultdict(list)
    for row in cursor.fetchall():
        if row["standard_ingredient_id"] in standards:
            aliases[row["standard_ingredient_id"]].extend(
                _split_grouped_alias_names(row.get("alias_names"))
            )
    return standards, aliases


def _shortlist(
    raw_name: str,
    standards: dict[str, dict[str, Any]],
    aliases: dict[str, list[str]],
    size: int,
) -> list[dict[str, Any]]:
    ranked = []
    for standard_id, standard in standards.items():
        names = [standard["standard_name"], *aliases.get(standard_id, [])]
        scored_names = sorted(
            ((_similarity(raw_name, name), name) for name in names), reverse=True
        )
        lexical_score, matched_name = scored_names[0]
        ranked.append(
            {
                "standard_ingredient_id": standard_id,
                "standard_name": standard["standard_name"],
                "ingredient_family": standard.get("ingredient_family"),
                "source_type": standard.get("source_type"),
                "animal_source": standard.get("animal_source"),
                "primary_nutrition_role": standard.get("primary_nutrition_role"),
                "closest_name": matched_name,
                "lexical_similarity": lexical_score,
            }
        )
    return sorted(
        ranked,
        key=lambda row: (-row["lexical_similarity"], row["standard_ingredient_id"]),
    )[:size]


def _load_candidates(cursor, *, limit: int) -> list[dict[str, Any]]:
    sql = """
      SELECT c.candidate_id, c.raw_name, c.normalized_raw_name, c.context
      FROM catfood_standard_ingredient_candidate c
      WHERE (c.status='model_error' OR (c.status='pending' AND c.model_result_json IS NULL)
             OR c.status IN ('new_standard_review','review_pending'))
        AND EXISTS (
          SELECT 1 FROM catfood_formula_ingredient_item i
          JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
          WHERE i.standard_ingredient_id IS NULL AND COALESCE(i.is_ignored,0)=0
            AND i.raw_name=c.raw_name
        )
      ORDER BY c.candidate_id
    """
    if limit > 0:
        sql += " LIMIT %s"
        cursor.execute(sql, (limit,))
    else:
        cursor.execute(sql)
    return [dict(row) for row in cursor.fetchall()]


def _ensure_candidates_for_current_items(cursor) -> None:
    cursor.execute(
        """
        SELECT i.raw_name, MAX(f.normalized_ingredient_composition) context
        FROM catfood_formula_ingredient_item i
        JOIN catfood_standard_formula f ON f.formula_id=i.formula_id
        WHERE i.standard_ingredient_id IS NULL AND COALESCE(i.is_ignored,0)=0
        GROUP BY i.raw_name
        """
    )
    for row in cursor.fetchall():
        normalized = _normalize_ingredient_key(row["raw_name"])
        if not normalized:
            continue
        cursor.execute(
            """
            INSERT INTO catfood_standard_ingredient_candidate(
              raw_name, normalized_raw_name, context, status
            ) VALUES(%s,%s,%s,'pending')
            ON DUPLICATE KEY UPDATE
              raw_name=IF(status LIKE 'discarded_%%',raw_name,VALUES(raw_name)),
              context=VALUES(context),
              status=IF(status='out_of_scope','pending',status)
            """,
            (row["raw_name"], normalized, str(row.get("context") or "")[:2000] or None),
        )


def _call_model(client: OpenAI, model: str, payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"items": payload}, ensure_ascii=False)},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    parsed = _extract_json(response.choices[0].message.content or "")
    results = parsed.get("results")
    if not isinstance(results, list):
        raise ValueError("模型 JSON 缺少 results 数组")
    return [row for row in results if isinstance(row, dict)]


def match_candidates(
    *, apply: bool, limit: int, batch_size: int, shortlist_size: int, auto_threshold: float
) -> dict[str, Any]:
    cfg = get_qwen_config()
    if not cfg["api_key"]:
        raise ValueError("缺少 QWEN_API_KEY/DASHSCOPE_API_KEY")
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60.0)

    counters = defaultdict(int)
    with _connect() as conn, conn.cursor() as cursor:
        _ensure_candidates_for_current_items(cursor)
        standards, aliases = _load_master(cursor)
        candidates = _load_candidates(cursor, limit=limit)
        prepared = []
        shortlist_by_candidate: dict[int, list[dict[str, Any]]] = {}
        for candidate in candidates:
            shortlist = _shortlist(candidate["raw_name"], standards, aliases, shortlist_size)
            shortlist_by_candidate[int(candidate["candidate_id"])] = shortlist
            prepared.append(
                {
                    "candidate_id": int(candidate["candidate_id"]),
                    "raw_name": candidate["raw_name"],
                    "candidates": shortlist,
                }
            )

        for offset in range(0, len(prepared), batch_size):
            batch = prepared[offset : offset + batch_size]
            try:
                results = _call_model(client, cfg["model"], batch)
                by_id = {int(row.get("candidate_id")): row for row in results if row.get("candidate_id")}
                for item in batch:
                    candidate_id = item["candidate_id"]
                    result = by_id.get(candidate_id)
                    if not result:
                        raise ValueError(f"模型遗漏 candidate_id={candidate_id}")
                    decision = str(result.get("decision") or "uncertain")
                    selected_id = str(result.get("standard_ingredient_id") or "") or None
                    try:
                        model_confidence = max(0.0, min(1.0, float(result.get("confidence") or 0)))
                    except (TypeError, ValueError):
                        model_confidence = 0.0
                    allowed = {
                        row["standard_ingredient_id"]: row
                        for row in shortlist_by_candidate[candidate_id]
                    }
                    selected = allowed.get(selected_id or "")
                    lexical = float(selected.get("lexical_similarity") or 0) if selected else 0.0
                    auto_approved = bool(
                        decision == "match_existing"
                        and selected
                        and model_confidence >= auto_threshold
                        and lexical >= 0.25
                    )
                    discard_decisions = {
                        "discard_noise", "discard_non_ingredient", "discard_compound"
                    }
                    if decision in discard_decisions and model_confidence >= 0.85 and _valid_discard(item["raw_name"], decision):
                        status, reviewer = decision.replace("discard_", "discarded_"), f"llm:{cfg['model']}"
                        counters[status] += 1
                    elif auto_approved:
                        status, reviewer = "approved", f"llm:{cfg['model']}"
                        cursor.execute(
                            "SELECT alias_names FROM catfood_standard_ingredient_alias "
                            "WHERE standard_ingredient_id=%s FOR UPDATE", (selected_id,)
                        )
                        alias_row = cursor.fetchone()
                        existing_aliases = _split_grouped_alias_names(
                            alias_row.get("alias_names") if alias_row else None
                        )
                        if item["raw_name"] not in existing_aliases:
                            existing_aliases.append(item["raw_name"])
                        cursor.execute(
                            """INSERT INTO catfood_standard_ingredient_alias(
                              standard_ingredient_id,standard_name,alias_names)
                              VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE
                              alias_names=VALUES(alias_names)""",
                            (selected_id, selected["standard_name"], "、".join(existing_aliases)),
                        )
                        counters["auto_approved"] += 1
                    elif decision == "need_new_standard" or lexical < 0.25:
                        status, reviewer = "new_standard_reviewed", None
                        counters["new_standard_review"] += 1
                    else:
                        status, reviewer = "review_pending_reviewed", None
                        counters["pending_review"] += 1
                    audit = {
                        "model": cfg["model"], "decision": decision,
                        "model_confidence": model_confidence,
                        "lexical_similarity": lexical,
                        "reason": str(result.get("reason") or "")[:500],
                        "selected": selected, "shortlist": shortlist_by_candidate[candidate_id],
                    }
                    cursor.execute(
                        """
                        UPDATE catfood_standard_ingredient_candidate
                        SET suggested_standard_ingredient_id=%s,
                            suggested_standard_name=%s, model_result_json=%s,
                            status=%s, reviewer=%s, review_note=%s,
                            reviewed_at=CASE WHEN %s='approved' THEN NOW() ELSE NULL END
                        WHERE candidate_id=%s
                        """,
                        (
                            selected_id if selected else None,
                            selected.get("standard_name") if selected else None,
                            json.dumps(audit, ensure_ascii=False), status, reviewer,
                            audit["reason"], status, candidate_id,
                        ),
                    )
            except Exception as exc:
                counters["model_errors"] += len(batch)
                for item in batch:
                    cursor.execute(
                        "UPDATE catfood_standard_ingredient_candidate "
                        "SET status='model_error', review_note=%s WHERE candidate_id=%s",
                        (str(exc)[:1000], item["candidate_id"]),
                    )
            if apply:
                conn.commit()
            else:
                conn.rollback()
            time.sleep(0.2)

    return {
        "applied": apply, "model": cfg["model"], "candidate_count": len(candidates),
        **dict(counters),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--shortlist-size", type=int, default=12)
    parser.add_argument("--auto-threshold", type=float, default=0.94)
    args = parser.parse_args()
    result = match_candidates(
        apply=args.apply, limit=max(0, args.limit), batch_size=max(1, args.batch_size),
        shortlist_size=max(3, args.shortlist_size), auto_threshold=args.auto_threshold,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
