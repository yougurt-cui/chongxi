"""宠物知识库二次蒸馏服务层（移植自 pet_knowledge_second_stage/distill.py）。

从已有 knowledge_base 原子知识出发，蒸馏为「标准问题 → 问题表现 → 原因/对应办法 → 风险与争议」
知识卡，保留原始知识 ID 可追溯。三张表：

- kd_claims          模型从原子知识提取的独立论点（original_id、role、problem_key、条件、证据状态）
- kd_problem_guides  按 problem_key 组装的知识卡（症状 / 原因-办法对 / 风险 / 争议 / 审核状态）
- kd_topic_runs      主题级内容指纹，重复运行自动跳过；内容变更触发该主题重算

约定：
- 与 knowledge_base 同库（csv_labeling），便于 join；
- LLM 走 dashscope 兼容接口（app_config.get_qwen_config，可用 QWEN_MODEL/DASHSCOPE_API_KEY 覆盖）；
- mode="demo" 为无 API 的规则链路验证，不触碰 knowledge_base，只写 kd_* 表；
- 正式模式输出是内容草稿，不是事实验证；专业已验证仅可由人工审核（approve）授予。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
import requests
from sqlalchemy import (Column, DateTime, Integer, String, Text, UniqueConstraint,
                        create_engine, text)
from sqlalchemy.orm import Session, declarative_base

from app_config import get_mysql_config, get_qwen_config

Base = declarative_base()

VALID_ROLES = {"symptom", "cause", "action", "risk", "experience", "dispute"}
VALID_CLAIM_STATUS = {"待验证", "社区经验", "专业已验证", "有争议"}
VALID_EVIDENCE = ("待验证", "社区经验", "专业已验证", "有争议")
ELIGIBLE_SOURCE_STATUSES = ("已审核", "已通过", "已发布", "active", "approved", "published")
PIPELINE_VERSION = "2026-10-26.1"
MAX_SOURCE_ROWS = 200
MAX_GUIDE_GROUPS = 50


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def jd(x) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def jl(x, fallback):
    try:
        return json.loads(x) if x else fallback
    except (ValueError, TypeError):
        return fallback


def digest(x) -> str:
    return hashlib.sha256(jd(x).encode("utf-8")).hexdigest()


class KdClaim(Base):
    __tablename__ = "kd_claims"
    id = Column(Integer, primary_key=True)
    original_id = Column(Integer, nullable=False, index=True)
    topic = Column(String(80), nullable=False)
    subtopic = Column(String(120), nullable=False)
    problem_key = Column(String(180), nullable=False, index=True)
    problem = Column(String(300), nullable=False)
    role = Column(String(30), nullable=False)  # symptom / cause / action / risk / experience / dispute
    claim = Column(Text, nullable=False)
    conditions_json = Column(Text, nullable=False, default="{}")
    claim_status = Column(String(30), nullable=False, default="待验证")
    source_hash = Column(String(64), nullable=False)


class KdGuide(Base):
    __tablename__ = "kd_problem_guides"
    id = Column(Integer, primary_key=True)
    topic = Column(String(80), nullable=False)
    subtopic = Column(String(120), nullable=False)
    problem_key = Column(String(180), nullable=False)
    problem = Column(String(300), nullable=False)
    symptoms_json = Column(Text, nullable=False, default="[]")
    pairs_json = Column(Text, nullable=False, default="[]")
    risks_json = Column(Text, nullable=False, default="[]")
    disputes_json = Column(Text, nullable=False, default="[]")
    conditions_json = Column(Text, nullable=False, default="{}")
    source_ids_json = Column(Text, nullable=False, default="[]")
    evidence_status = Column(String(30), nullable=False, default="待验证")
    review_status = Column(String(30), nullable=False, default="待审核")
    content_hash = Column(String(64), nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("topic", "subtopic", "problem_key", name="uq_kd_guide_key"),)


class KdTopicRun(Base):
    __tablename__ = "kd_topic_runs"
    topic = Column(String(80), primary_key=True)
    subtopic = Column(String(120), primary_key=True)
    source_hash = Column(String(64), nullable=False)
    processed_at = Column(DateTime)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

SYSTEM_EXTRACT = """你是宠物社区知识库二次蒸馏工程的“观点整理器”。
输入为若干原子知识。只提取其中明确表达、可追溯的观点，不得使用外部常识来补全。
每个观点写成一个 JSON 对象，输出 {"items":[...]}。
对象字段：original_id(int), problem_key(str), problem(str), role(str), claim(str), conditions(object), claim_status(str)。
role只能取 symptom,cause,action,risk,experience,dispute。
claim_status只能是 待验证/社区经验/专业已验证/有争议；除非原始证据已标注“专业已验证”，不得输出专业已验证。
统一问题名和key，示例：幼猫不愿意吃干粮 -> kitten_refuses_dry_food；幼猫吃后吐 -> kitten_postmeal_vomiting；幼猫长肉慢 -> kitten_growth_concern。
一条原子知识可能同时表达原因和建议，请拆成多条，不要无依据推断。品牌体验归 experience；带绝对化推断或争议的观点归 dispute。
每条都必须携带原始original_id，且ID必须来自本次输入。对不相关、纯广告、无信息的内容不输出。只输出JSON。"""

SYSTEM_GUIDE = """你是知识整合器，只能使用输入论点，不能自行增加医学原因、治疗办法或风险信号。
输出 JSON：{"problem":str, "symptoms":[{"text":str,"source_ids":[int]}],
"cause_action_pairs":[{"cause":str|null,"action":str|null,"cause_source_ids":[int],"action_source_ids":[int],"status":"待验证"}],
"risks":[{"text":str,"source_ids":[int]}], "disputes":[{"text":str,"source_ids":[int]}],"conditions":{}}。
只把证据明确支持、且逻辑上关联的原因/解决办法组成一对；不能强配。
只有原因时action=null，只有办法时cause=null。不允许空想来源ID，不允许将用户体验升格为事实，不能输出未在输入中出现的风险规则。
同义观点合并，同一观点source_ids取并集；保留冲突和限制条件。结果为待专业审核草稿。只输出JSON。"""


def _llm(cfg: dict, system: str, payload: dict) -> dict:
    if not cfg.get("api_key") or not cfg.get("model"):
        raise RuntimeError("缺少大模型配置（DASHSCOPE_API_KEY/QWEN_MODEL 或 vendor config）")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    data = {
        "model": cfg["model"],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": jd(payload)},
        ],
    }
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                url, headers={"Authorization": "Bearer " + cfg["api_key"]}, json=data, timeout=120,
            )
            resp.raise_for_status()
            out = resp.json()["choices"][0]["message"]["content"]
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", out.strip()))
        except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise last_exc  # pragma: no cover


# ---------------------------------------------------------------------------
# Demo 规则链路（无 API 时的数据流验证，不用于实际内容生产）
# ---------------------------------------------------------------------------

DEMO_PATTERNS = [
    ("拒食干粮", "kitten_refuses_dry_food", "幼猫不愿意吃干粮", "symptom", "幼猫不愿意接受干粮"),
    ("泡软猫粮", "kitten_refuses_dry_food", "幼猫不愿意吃干粮", "action", "社区建议调整干粮质地"),
    ("食物质地", "kitten_refuses_dry_food", "幼猫不愿意吃干粮", "cause", "食物质地可能影响接受度"),
    ("剧烈奔跑", "kitten_postmeal_vomiting", "幼猫进食后出现呕吐", "cause", "进食后剧烈活动被认为可能与呕吐有关"),
    ("长肉", "kitten_growth_concern", "幼猫长肉慢怎么办", "experience", "用户反馈某猫粮与长肉同时出现"),
    ("纯肉猫粮", "kitten_growth_concern", "幼猫长肉慢怎么办", "dispute", "社区声称纯肉粮可促进长肉，尚缺证据"),
]


def _demo_extract(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        for token, key, problem, role, statement in DEMO_PATTERNS:
            if token in row["content"]:
                out.append(dict(
                    original_id=row["id"], problem_key=key, problem=problem, role=role,
                    claim=statement, conditions={"species": "cat", "life_stage": "kitten"},
                    claim_status="待验证",
                ))
    return out


# ---------------------------------------------------------------------------
# 抽取 / 组装
# ---------------------------------------------------------------------------

def _normalize_items(raw, rows: list[dict]) -> list[dict]:
    lookup = {r["id"]: r for r in rows}
    items = []
    for x in raw if isinstance(raw, list) else []:
        try:
            sid = int(x["original_id"])
            key = str(x["problem_key"]).strip().lower()
            if sid not in lookup or not re.fullmatch(r"[a-z0-9_]{4,180}", key):
                continue
            role = x["role"]
            if role not in VALID_ROLES:
                continue
            claim = str(x["claim"]).strip()
            if not claim or len(claim) > 800:
                continue
            status = x.get("claim_status", "待验证")
            src = lookup[sid]
            if status == "专业已验证" and src.get("evidence_status") != "专业已验证":
                status = "待验证"
            if status not in VALID_CLAIM_STATUS:
                status = "待验证"
            conditions = x.get("conditions")
            items.append(dict(
                original_id=sid, topic=src["topic"], subtopic=src["subtopic"], problem_key=key,
                problem=str(x["problem"])[:300], role=role, claim=claim,
                conditions_json=jd(conditions if isinstance(conditions, dict) else {}),
                claim_status=status, source_hash=digest(src),
            ))
        except (ValueError, TypeError, KeyError):
            continue
    return items


def _existing_rows(db, topic: str, subtopic: str) -> list[dict]:
    placeholders = ",".join(f":status_{i}" for i in range(len(ELIGIBLE_SOURCE_STATUSES)))
    sql = ("SELECT id, content, topic, subtopic, evidence_status, status FROM knowledge_base "
           f"WHERE topic=:t AND subtopic=:s AND status IN ({placeholders}) ORDER BY id")
    params = {"t": topic, "s": subtopic}
    params.update({f"status_{i}": status for i, status in enumerate(ELIGIBLE_SOURCE_STATUSES)})
    return [dict(r) for r in db.execute(text(sql), params).mappings().all()]


def _extract(rows: list[dict], cfg: dict, mode: str, chunk: int) -> list[dict]:
    result: list[dict] = []
    for offset in range(0, len(rows), max(1, chunk)):
        batch = rows[offset:offset + chunk]
        if mode == "demo":
            raw_items = _demo_extract(batch)
        else:
            raw_items = _llm(cfg, SYSTEM_EXTRACT, {"knowledge": batch}).get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("模型提取结构错误: items 应为数组")
        result.extend(_normalize_items(raw_items, batch))
    # 唯一化，避免相同原子论点重复记录
    return list({(x["original_id"], x["problem_key"], x["role"], x["claim"]): x for x in result}.values())


def _safe_guide(raw: dict, claims: list) -> dict:
    by_id: dict[int, list] = defaultdict(list)
    for claim in claims:
        by_id[claim.original_id].append(claim)

    def valid_ids(values, roles: set[str]) -> list[int]:
        ids = set()
        for value in values if isinstance(values, list) else []:
            if not str(value).isdigit():
                continue
            source_id = int(value)
            if any(claim.role in roles for claim in by_id.get(source_id, [])):
                ids.add(source_id)
        return sorted(ids)

    def provenance(ids: list[int], roles: set[str]) -> list[dict]:
        return [
            {"id": source_id, "claim": claim.claim}
            for source_id in ids
            for claim in by_id[source_id]
            if claim.role in roles
        ]

    def entries(key: str, roles: set[str], text_key: str = "text") -> list[dict]:
        out = []
        for item in raw.get(key, []) if isinstance(raw.get(key), list) else []:
            if not isinstance(item, dict):
                continue
            ids = valid_ids(item.get("source_ids", []), roles)
            if ids and item.get(text_key):
                out.append({"text": str(item[text_key])[:700], "source_ids": ids,
                            "source_claims": provenance(ids, roles)})
        return out

    pairs = []
    for x in raw.get("cause_action_pairs", []) if isinstance(raw.get("cause_action_pairs"), list) else []:
        if not isinstance(x, dict):
            continue
        cids = valid_ids(x.get("cause_source_ids", []), {"cause"})
        aids = valid_ids(x.get("action_source_ids", []), {"action"})
        cause = x.get("cause") if cids else None
        action = x.get("action") if aids else None
        if cause or action:
            pairs.append({"cause": cause, "action": action, "cause_source_ids": cids,
                          "action_source_ids": aids, "cause_sources": provenance(cids, {"cause"}),
                          "action_sources": provenance(aids, {"action"}), "status": "待验证"})
    conditions = raw.get("conditions")
    return {
        "problem": str(raw.get("problem") or claims[0].problem)[:300],
        "symptoms": entries("symptoms", {"symptom"}),
        "pairs": pairs,
        "risks": entries("risks", {"risk"}),
        "disputes": entries("disputes", {"dispute", "experience"}),
        "conditions": conditions if isinstance(conditions, dict) else {},
    }


def _assemble(claims: list, cfg: dict, mode: str) -> dict:
    if mode == "demo":
        # 演示时不补不存在于原子知识的内容，只将同一 role 同一陈述合并
        raw = {"problem": claims[0].problem, "symptoms": [], "cause_action_pairs": [],
               "risks": [], "disputes": [], "conditions": {}}
        for c in claims:
            item = {"text": c.claim, "source_ids": [c.original_id]}
            if c.role == "symptom":
                raw["symptoms"].append(item)
            elif c.role == "risk":
                raw["risks"].append(item)
            elif c.role in ("dispute", "experience"):
                raw["disputes"].append(item)
            elif c.role == "cause":
                raw["cause_action_pairs"].append({"cause": c.claim, "action": None,
                                                  "cause_source_ids": [c.original_id], "action_source_ids": []})
            elif c.role == "action":
                raw["cause_action_pairs"].append({"cause": None, "action": c.claim,
                                                  "cause_source_ids": [], "action_source_ids": [c.original_id]})
    else:
        raw = _llm(cfg, SYSTEM_GUIDE, {"claims": [
            {"original_id": c.original_id, "role": c.role, "claim": c.claim,
             "conditions": jl(c.conditions_json, {})} for c in claims]})
    return _safe_guide(raw, claims)


# ---------------------------------------------------------------------------
# 引擎 / 表初始化
# ---------------------------------------------------------------------------

def _engine():
    cfg = get_mysql_config()
    url = (f"mysql+pymysql://{cfg['user']}:{cfg['password']}@{cfg['host']}:{cfg['port']}"
           f"/{cfg['database']}?charset={cfg['charset']}")
    return create_engine(url, future=True, pool_pre_ping=True)


def _llm_cfg(model: str | None = None) -> dict:
    cfg = get_qwen_config({"model": model} if model else None)
    return {
        "base_url": (cfg.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/"),
        "api_key": cfg.get("api_key"),
        "model": cfg.get("model") or "qwen-plus",
    }


def ensure_tables(engine) -> None:
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def run_distill(topic: str, subtopic: str, mode: str = "llm", chunk: int = 20,
                model: str | None = None) -> dict:
    """对指定 topic/subtopic 的 knowledge_base 做二次蒸馏；无变更时 skipped=True。"""
    if mode not in {"demo", "llm"}:
        raise ValueError("mode 只能是 demo 或 llm")
    chunk = max(1, min(int(chunk), 50))
    engine = _engine()
    ensure_tables(engine)
    cfg = _llm_cfg(model)
    with Session(engine) as db:
        rows = _existing_rows(db, topic, subtopic)
        if not rows:
            return {"input": 0, "claims": 0, "guides": 0, "skipped": True,
                    "message": f"knowledge_base 中无 topic={topic} subtopic={subtopic} 的数据"}
        if len(rows) > MAX_SOURCE_ROWS:
            raise ValueError(
                f"符合条件的知识有 {len(rows)} 条，超过单次上限 {MAX_SOURCE_ROWS}；请细分 subtopic 后重试"
            )
        h = digest({
            "pipeline_version": PIPELINE_VERSION,
            "mode": mode,
            "model": cfg.get("model") if mode == "llm" else "demo",
            "chunk": chunk,
            "extract_prompt": digest(SYSTEM_EXTRACT),
            "guide_prompt": digest(SYSTEM_GUIDE),
            "rows": rows,
        })
        prev = db.execute(
            text("SELECT source_hash FROM kd_topic_runs WHERE topic=:t AND subtopic=:s"),
            {"t": topic, "s": subtopic},
        ).scalar()
        if prev == h:
            return {"input": len(rows), "claims": 0, "guides": 0, "skipped": True}
        # 单次事务内更新；LLM 异常则保留旧版知识卡
        objects = _extract(rows, cfg, mode, chunk)
        if not objects:
            raise ValueError("二次蒸馏未产生有效论点；已保留该主题原有结果，请检查模型输出或源数据")
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for x in objects:
            groups[(x["topic"], x["subtopic"], x["problem_key"])].append(x)
        if len(groups) > MAX_GUIDE_GROUPS:
            raise ValueError(
                f"模型生成了 {len(groups)} 个问题组，超过单次上限 {MAX_GUIDE_GROUPS}；"
                "已保留原有结果，请缩小数据范围或统一 problem_key"
            )
        built = {key: _assemble([SimpleNamespace(**x) for x in vals], cfg, mode)
                 for key, vals in groups.items()}
        db.execute(text("DELETE FROM kd_claims WHERE topic=:t AND subtopic=:s"), {"t": topic, "s": subtopic})
        for x in objects:
            db.add(KdClaim(**x))
        old_guides = db.query(KdGuide).filter_by(topic=topic, subtopic=subtopic).all()
        existing = {g.problem_key: g for g in old_guides}
        for key, guide in built.items():
            _, _, k = key
            claims = groups[key]
            ids = sorted({x["original_id"] for x in claims})
            payload = {"problem": guide["problem"], "symptoms": guide["symptoms"], "pairs": guide["pairs"],
                       "risks": guide["risks"], "disputes": guide["disputes"],
                       "conditions": guide["conditions"], "source_ids": ids}
            ph = digest(payload)
            g = existing.pop(k, None)
            if g is None:
                g = KdGuide(topic=topic, subtopic=subtopic, problem_key=k, problem=guide["problem"],
                            content_hash=ph)
                db.add(g)
            elif g.content_hash != ph:
                g.review_status = "待审核"  # 新证据使旧审核失效
            g.problem = guide["problem"]
            g.symptoms_json = jd(guide["symptoms"])
            g.pairs_json = jd(guide["pairs"])
            g.risks_json = jd(guide["risks"])
            g.disputes_json = jd(guide["disputes"])
            g.conditions_json = jd(guide["conditions"])
            g.source_ids_json = jd(ids)
            g.content_hash = ph
            g.evidence_status = "待验证" if g.review_status != "已审核" else g.evidence_status
        for ob in existing.values():
            db.delete(ob)
        db.execute(text("DELETE FROM kd_topic_runs WHERE topic=:t AND subtopic=:s"), {"t": topic, "s": subtopic})
        db.execute(text("INSERT INTO kd_topic_runs (topic,subtopic,source_hash,processed_at) VALUES (:t,:s,:h,:d)"),
                   {"t": topic, "s": subtopic, "h": h, "d": utcnow()})
        db.commit()
        return {"input": len(rows), "claims": len(objects), "guides": len(built), "skipped": False}


def list_guides(topic: str | None = None, subtopic: str | None = None,
                review_status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    """知识卡列表，可按 topic/subtopic/review_status 过滤。"""
    engine = _engine()
    ensure_tables(engine)
    where, params = ["1=1"], {"limit": max(1, min(int(limit), 500)), "offset": max(0, int(offset))}
    if topic:
        where.append("topic = :topic"); params["topic"] = topic
    if subtopic:
        where.append("subtopic = :subtopic"); params["subtopic"] = subtopic
    if review_status:
        where.append("review_status = :review_status"); params["review_status"] = review_status
    sql = (f"SELECT * FROM kd_problem_guides WHERE {' AND '.join(where)} "
           f"ORDER BY topic, subtopic, id LIMIT :limit OFFSET :offset")
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    result = []
    for r in rows:
        d = dict(r)
        d["symptoms"] = jl(d.pop("symptoms_json"), [])
        d["cause_action_pairs"] = jl(d.pop("pairs_json"), [])
        d["risks"] = jl(d.pop("risks_json"), [])
        d["disputes"] = jl(d.pop("disputes_json"), [])
        d["conditions"] = jl(d.pop("conditions_json"), {})
        d["source_knowledge_ids"] = jl(d.pop("source_ids_json"), [])
        if isinstance(d.get("updated_at"), datetime):
            d["updated_at"] = d["updated_at"].isoformat(sep=" ", timespec="seconds")
        result.append(d)
    return result


def list_claims(topic: str | None = None, subtopic: str | None = None,
                problem_key: str | None = None, original_id: int | None = None,
                limit: int = 200, offset: int = 0) -> list[dict]:
    """论点明细（追溯用），可按 problem_key 或原始知识 ID 过滤。"""
    engine = _engine()
    ensure_tables(engine)
    where, params = ["1=1"], {"limit": max(1, min(int(limit), 1000)), "offset": max(0, int(offset))}
    if topic:
        where.append("topic = :topic"); params["topic"] = topic
    if subtopic:
        where.append("subtopic = :subtopic"); params["subtopic"] = subtopic
    if problem_key:
        where.append("problem_key = :problem_key"); params["problem_key"] = problem_key
    if original_id:
        where.append("original_id = :original_id"); params["original_id"] = int(original_id)
    sql = (f"SELECT * FROM kd_claims WHERE {' AND '.join(where)} "
           f"ORDER BY problem_key, original_id, id LIMIT :limit OFFSET :offset")
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(sql), params).mappings().all()]
    for r in rows:
        r["conditions"] = jl(r.pop("conditions_json"), {})
    return rows


def approve_guide(guide_id: int, evidence: str) -> dict:
    """人工审核入口：仅改变状态，不具备自动验证能力；专业已验证须由专业人员执行。"""
    if evidence not in VALID_EVIDENCE:
        raise ValueError(f"evidence 只能是 {'/'.join(VALID_EVIDENCE)}")
    engine = _engine()
    ensure_tables(engine)
    with Session(engine) as db:
        guide = db.get(KdGuide, int(guide_id))
        if guide is None:
            raise ValueError(f"知识卡不存在: {guide_id}")
        guide.review_status = "已审核"
        guide.evidence_status = evidence
        db.commit()
        return {"guide_id": guide.id, "problem_key": guide.problem_key,
                "review_status": guide.review_status, "evidence_status": guide.evidence_status}


def stats() -> dict:
    """kd 三表计数与知识卡状态分布。"""
    engine = _engine()
    ensure_tables(engine)
    with engine.connect() as conn:
        def count(table: str) -> int:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)

        guides = count("kd_problem_guides")
        review = [dict(r) for r in conn.execute(
            text("SELECT review_status, COUNT(*) n FROM kd_problem_guides GROUP BY review_status")).mappings()]
        evidence = [dict(r) for r in conn.execute(
            text("SELECT evidence_status, COUNT(*) n FROM kd_problem_guides GROUP BY evidence_status")).mappings()]
        topics = [dict(r) for r in conn.execute(
            text("SELECT topic, subtopic, COUNT(*) n FROM kd_problem_guides GROUP BY topic, subtopic "
                 "ORDER BY n DESC LIMIT 20")).mappings()]
        roles = [dict(r) for r in conn.execute(
            text("SELECT role, COUNT(*) n FROM kd_claims GROUP BY role ORDER BY n DESC")).mappings()]
        runs = [dict(r) for r in conn.execute(text("SELECT * FROM kd_topic_runs")).mappings()]
        for r in runs:
            r["processed_at"] = r["processed_at"].isoformat(sep=" ", timespec="seconds") if r["processed_at"] else None
        claims_n = count("kd_claims")
        runs_n = count("kd_topic_runs")
    return {"claims": claims_n, "guides": guides, "topic_runs": runs_n,
            "review_status": review, "evidence_status": evidence, "topics": topics,
            "roles": roles, "runs": runs}
