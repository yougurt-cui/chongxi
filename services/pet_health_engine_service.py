"""健康关注引擎核心逻辑 + 小佩设备数据桥接。

移植自 pet_health_engine_v2.zip 的 service.py / main.py，改动：
  - FastAPI 依赖全部去掉，改为纯 service 函数，供 Flask Blueprint 调用；
  - 新增 sync_petkit_daily_metrics()：调 petkit_service 采集小佩设备快照并登记设备，
    再跑 analyze —— 原始硬件字段到健康指标的解析规则暂不启用。
安全口径（与原版一致，必须遵守）：
  - 喂食器只有“出粮”没有“进食克数”，food_intake 只能来自人工/称重，不能冒充；
  - 只用 valid + confirmed + daily 的数据参与基线；
  - 当日缺数据或历史不足 MIN_DAYS 天，结果只能是 insufficient_data，不能给“正常”。
"""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from services.pet_health_models import (
    Base,
    HealthAttention,
    HealthDirectionRule,
    MetricAnomaly,
    MetricBaseline,
    MetricRecord,
    PetDevice,
    get_db_url,
    make_engine,
    make_session,
    migrate_schema,
)
from services.pet_health_rules import DIRECTIONS, METRICS, REQUIRED, RULE_VERSION, THRESHOLDS

WINDOW = 14
MIN_DAYS = 5

CN_TZ = timezone(timedelta(hours=8))

_engine = None
_SessionLocal = None


def get_session():
    """惰性建引擎；PET_HEALTH_DATABASE_URL 可切换 MySQL。"""
    global _engine, _SessionLocal
    if _SessionLocal is None:
        _engine = make_engine(get_db_url())
        Base.metadata.create_all(_engine)
        migrate_schema(_engine)
        _SessionLocal = make_session(_engine)
    return _SessionLocal()


def seed_rules(db):
    for code, cfg in DIRECTIONS.items():
        row = db.scalar(select(HealthDirectionRule).where(HealthDirectionRule.direction_code == code))
        if row is None:
            row = HealthDirectionRule(direction_code=code)
            db.add(row)
        row.direction_name = cfg["name"]
        row.core_metrics = cfg["core"]
        row.auxiliary_metrics = cfg["aux"]
        row.rule_config = {
            "required_for_normal": REQUIRED[code],
            "thresholds": THRESHOLDS,
            "min_baseline_days": MIN_DAYS,
        }
        row.abnormal_message = cfg["abnormal"]
        row.normal_message = cfg["normal"]
        row.missing_message = cfg["missing"]
        row.rule_version = RULE_VERSION
        row.enabled = True
    db.commit()


def add_metric(db: Session, payload: dict):
    """新增一条归一化指标记录（与原 FastAPI Reading 校验一致）。"""
    metric_code = str(payload.get("metric_code") or "").strip()
    if metric_code not in METRICS:
        raise ValueError(f"未知指标: {metric_code!r}，可选: {sorted(METRICS)}")
    if METRICS[metric_code][1] != payload.get("unit"):
        raise ValueError(f"单位不匹配: {metric_code} 要求 {METRICS[metric_code][1]!r}")
    period_type = payload.get("period_type", "daily")
    data_quality = payload.get("data_quality", "valid")
    attribution_status = payload.get("attribution_status", "confirmed")
    if period_type not in {"daily", "event"}:
        raise ValueError("period_type 仅支持 daily / event")
    if data_quality not in {"valid", "estimated", "invalid"}:
        raise ValueError("data_quality 仅支持 valid / estimated / invalid")
    if attribution_status not in {"confirmed", "shared", "unknown"}:
        raise ValueError("attribution_status 仅支持 confirmed / shared / unknown")
    pet_id = str(payload.get("pet_id") or "").strip()
    if not pet_id:
        raise ValueError("必须传 pet_id")
    metric_value = float(payload["metric_value"])
    if metric_value < 0:
        raise ValueError("metric_value 不能为负")
    record_time = payload.get("record_time")
    if isinstance(record_time, str):
        record_time = datetime.fromisoformat(record_time)
    record_time = record_time or datetime.now(CN_TZ).replace(tzinfo=None)
    row = MetricRecord(
        pet_id=pet_id,
        device_id=payload.get("device_id"),
        metric_code=metric_code,
        metric_value=metric_value,
        unit=payload["unit"],
        record_time=record_time,
        period_type=period_type,
        data_source=payload.get("data_source", "manual"),
        data_quality=data_quality,
        attribution_status=attribution_status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "pet_id": pet_id, "metric_code": metric_code}


def analyze(db: Session, pet_id: str, target_date: date):
    seed_rules(db)
    start = target_date - timedelta(days=WINDOW)
    rows = db.scalars(
        select(MetricRecord).where(
            MetricRecord.pet_id == pet_id,
            MetricRecord.record_time >= datetime.combine(start, datetime.min.time()),
            MetricRecord.record_time < datetime.combine(target_date + timedelta(days=1), datetime.min.time()),
            MetricRecord.data_quality == "valid",
            MetricRecord.attribution_status == "confirmed",
            MetricRecord.period_type == "daily",
        )
    ).all()
    # 同日同指标跨来源冲突时不静默选值；同一来源的重复采集才取最新一条。
    daily = {}
    grouped = defaultdict(list)
    for r in rows:
        if r.metric_code in METRICS and r.unit == METRICS[r.metric_code][1] and r.metric_value >= 0:
            grouped[(r.metric_code, r.record_time.date())].append(r)
    data_conflicts = []
    for key, candidates in grouped.items():
        sources = sorted({r.data_source or "unknown" for r in candidates})
        if len(sources) > 1:
            data_conflicts.append({
                "metric_code": key[0], "date": key[1].isoformat(), "data_sources": sources,
                "record_ids": sorted(r.id for r in candidates),
            })
            continue
        daily[key] = max(candidates, key=lambda x: (x.record_time, x.id))
    baselines = {}
    changes = {}
    present = {}
    for metric in METRICS:
        history = [r.metric_value for (m, d), r in daily.items() if m == metric and d < target_date]
        today = daily.get((metric, target_date))
        if len(history) >= MIN_DAYS:
            base = median(history)
            baselines[metric] = {"value": base, "low": min(history), "high": max(history), "days": len(history)}
            if today:
                present[metric] = today.metric_value
                if base > 0 and metric in THRESHOLDS:
                    change = (today.metric_value - base) / base
                    if abs(change) >= THRESHOLDS[metric]:
                        changes[metric] = {
                            "value": today.metric_value,
                            "baseline": base,
                            "change_percent": round(change * 100, 1),
                            "source": today.data_source,
                        }
        # 有今日数据却不足历史基线，应归入待评估，而不是正常
    db.execute(delete(MetricBaseline).where(MetricBaseline.pet_id == pet_id, MetricBaseline.target_date == target_date))
    db.execute(
        delete(MetricAnomaly).where(
            MetricAnomaly.pet_id == pet_id,
            MetricAnomaly.target_date == target_date,
            MetricAnomaly.rule_version == RULE_VERSION,
        )
    )
    db.execute(
        delete(HealthAttention).where(
            HealthAttention.pet_id == pet_id,
            HealthAttention.target_date == target_date,
            HealthAttention.rule_version == RULE_VERSION,
        )
    )
    now = datetime.utcnow()
    for m, b in baselines.items():
        db.add(
            MetricBaseline(
                pet_id=pet_id, metric_code=m, baseline_period=WINDOW, baseline_value=b["value"],
                lower_bound=b["low"], upper_bound=b["high"], valid_days=b["days"],
                calculated_at=now, baseline_version=RULE_VERSION, target_date=target_date,
            )
        )
    for m, c in changes.items():
        db.add(
            MetricAnomaly(
                pet_id=pet_id, metric_code=m, current_value=c["value"], baseline_value=c["baseline"],
                change_percent=c["change_percent"], anomaly_type="increase" if c["change_percent"] > 0 else "decrease",
                duration_days=1, rule_version=RULE_VERSION, detected_at=now, target_date=target_date,
            )
        )
    results = []
    for code, cfg in DIRECTIONS.items():
        # 仅核心指标异常触发此方向；辅助指标仅展示证据，不能独立生成疾病方向结论
        triggered = [m for m in cfg["core"] if m in changes]
        missing = [m for m in REQUIRED[code] if m not in present]
        if triggered:
            status = "abnormal"
        elif not missing:
            status = "normal"
        else:
            status = "insufficient_data"
        evidence = [{"metric_code": m, "metric_name": METRICS[m][0], **changes[m]} for m in triggered]
        all_sources = sorted(
            {
                r.data_source
                for (m, d), r in daily.items()
                if m in set(cfg["core"] + cfg["aux"]) and d == target_date and r.data_source
            }
        )
        level = "attention" if status == "abnormal" else "none"
        msg = cfg["abnormal" if status == "abnormal" else "normal" if status == "normal" else "missing"]
        results.append(
            {
                "direction_code": code,
                "direction_name": cfg["name"],
                "result_status": status,
                "attention_level": level,
                "evidence": evidence,
                "missing_metrics": missing,
                "data_sources": all_sources,
                "display_message": msg,
                "rule_version": RULE_VERSION,
                "target_date": target_date.isoformat(),
            }
        )
        db.add(
            HealthAttention(
                pet_id=pet_id, direction_code=code, result_status=status, evidence=evidence,
                missing_metrics=missing, data_sources=all_sources, display_message=msg,
                attention_level=level, rule_version=RULE_VERSION, analysis_time=now, target_date=target_date,
            )
        )
    db.commit()
    return {
        "pet_id": pet_id,
        "target_date": target_date.isoformat(),
        "rule_version": RULE_VERSION,
        "data_conflicts": data_conflicts,
        "directions": results,
        "notes": [
            "演示阈值非临床标准；体征异常不可作为疾病诊断",
            "排尿困难、呼吸费力等紧急症状须立即就医，不能等待设备规则判定",
        ],
    }


def latest_attention(pet_id: str):
    """读取某宠物最近一次分析的 5 类卡片。"""
    with get_session() as db:
        latest_day = db.scalar(
            select(HealthAttention.target_date)
            .where(HealthAttention.pet_id == pet_id)
            .order_by(HealthAttention.target_date.desc())
            .limit(1)
        )
        if latest_day is None:
            raise LookupError(f"该宠物尚无健康关注结果: {pet_id}")
        rows = (
            db.scalars(
                select(HealthAttention)
                .where(HealthAttention.pet_id == pet_id, HealthAttention.target_date == latest_day)
                .order_by(HealthAttention.id)
            )
            .all()
        )
        return {
            "pet_id": pet_id,
            "target_date": latest_day.isoformat(),
            "directions": [
                {
                    "direction_code": r.direction_code,
                    "result_status": r.result_status,
                    "evidence": r.evidence,
                    "missing_metrics": r.missing_metrics,
                    "data_sources": r.data_sources,
                    "display_message": r.display_message,
                }
                for r in rows
            ],
        }


def analyze_pet(pet_id: str, target_date=None):
    """API 入口：开 session 跑 analyze。target_date 缺省为今天（Asia/Shanghai）。"""
    target_date = target_date or datetime.now(CN_TZ).date()
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    with get_session() as db:
        return analyze(db, pet_id, target_date)


def add_metric_api(payload: dict):
    with get_session() as db:
        return add_metric(db, payload)


# ── 小佩设备桥接 ─────────────────────────────────────────────

_PETKIT_DEVICE_LABELS = {"Feeder": "小佩智能喂食器", "WaterFountain": "小佩智能饮水机"}


def _snapshot_payload(collect_result):
    """读回脱敏快照，同时保留原始快照的追溯元数据。"""
    for snap in collect_result.get("snapshots", []):
        export = snap.get("export_json")
        if export and Path(export).exists():
            try:
                return json.loads(Path(export).read_text(encoding="utf-8"))["data"], snap
            except (OSError, ValueError, KeyError):
                continue
    return None, None


def _upsert_petkit_device(db: Session, pet_id: str, device_key: str, device_class: str, device_name: str, when):
    uid = f"petkit-{device_key}"
    device = db.scalar(select(PetDevice).where(PetDevice.device_uid == uid))
    if device is None:
        device = PetDevice(pet_id=pet_id, device_uid=uid, attribution_status="unknown")
        db.add(device)
    elif device.pet_id != pet_id:
        raise ValueError(
            f"设备 {device_key} 已绑定宠物 {device.pet_id}，不能在采集时自动改绑为 {pet_id}"
        )
    device.device_brand = "小佩"
    device.device_type = device_class
    device.device_name = device_name or _PETKIT_DEVICE_LABELS.get(device_class, uid)
    device.connection_status = "connected"
    device.last_sync_time = when
    db.flush()
    return device


def sync_petkit_daily_metrics(
    db: Session, pet_id: str, device_id: str, target_date: date, attribution_status="shared"
):
    """采集小佩原始快照并登记设备；硬件字段到健康指标的解析规则暂留空。"""
    from services import petkit_service

    today = datetime.now(CN_TZ).date()
    if target_date != today:
        raise ValueError(f"实时设备采集只能写入当天 {today.isoformat()}，不能写入 {target_date.isoformat()}")
    if attribution_status not in {"confirmed", "shared", "unknown"}:
        raise ValueError("attribution_status 仅支持 confirmed / shared / unknown")

    collect_result = petkit_service.collect(device_id=str(device_id), save_json=True)
    snap = (collect_result.get("snapshots") or [{}])[0]
    payload, snapshot = _snapshot_payload(collect_result)
    if payload is None:
        raise ValueError(f"快照读取失败: device_id={device_id}")
    device_class = payload.get("device_class") or snap.get("device_class") or ""
    source = _PETKIT_DEVICE_LABELS.get(device_class, f"petkit-{device_id}")
    snapshot = dict(snapshot or {})
    snapshot["snapshot_db"] = str(petkit_service.DB_PATH)
    now = datetime.now(CN_TZ).replace(tzinfo=None)

    device_row = _upsert_petkit_device(
        db, pet_id, device_id, device_class, str(payload.get("name") or ""), now
    )
    device_row.attribution_status = attribution_status

    db.commit()
    return {
        "device_id": str(device_id),
        "device_class": device_class,
        "data_source": source,
        "attribution_status": attribution_status,
        "source_snapshot_id": snapshot.get("snapshot_id"),
        "synced_metrics": {},
        "mapping_status": "pending",
        "note": "原始快照已落库并登记设备；硬件字段到健康指标的解析规则暂未启用",
    }


def collect_and_analyze(pet_id: str, device_id: str, target_date=None, attribution_status="shared"):
    """总入口：采小佩 → 落指标 → 跑健康方向分析。"""
    target_date = target_date or datetime.now(CN_TZ).date()
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    with get_session() as db:
        sync = sync_petkit_daily_metrics(
            db, pet_id=pet_id, device_id=device_id, target_date=target_date,
            attribution_status=attribution_status,
        )
        analysis = analyze(db, pet_id, target_date)
    return {"sync": sync, "analysis": analysis}
