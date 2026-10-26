"""健康关注引擎测试（移植自 pet_health_engine_v2.zip 的 test_engine.py，适配本工程模块名）。"""

from datetime import date, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import pytest

from services.pet_health_engine_service import (
    CN_TZ,
    _upsert_petkit_device,
    analyze,
    sync_petkit_daily_metrics,
)
from services.pet_health_models import (
    Base,
    HealthAttention,
    HealthDirectionRule,
    MetricAnomaly,
    MetricBaseline,
    MetricRecord,
    PetDevice,
)
from services.pet_health_rules import METRICS
from services import pet_health_engine_service as health_service


def _put(db, pet, metric, value, day, quality="valid"):
    db.add(
        MetricRecord(
            pet_id=pet, metric_code=metric, metric_value=value, unit=METRICS[metric][1],
            record_time=datetime.combine(day, datetime.min.time()), data_source="测试设备",
            period_type="daily", data_quality=quality, attribution_status="confirmed",
        )
    )


def test_three_states_and_six_tables():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        d = date(2026, 10, 23)
        for n in range(1, 8):
            for m, v in [("food_intake", 65), ("day_activity", 120), ("food_frequency", 6),
                         ("night_activity", 60), ("stool_frequency", 2)]:
                _put(db, "x", m, v, d - timedelta(days=n))
        for m, v in [("food_intake", 40), ("day_activity", 75), ("food_frequency", 4),
                     ("night_activity", 60), ("stool_frequency", 2)]:
            _put(db, "x", m, v, d)
        db.commit()
        out = analyze(db, "x", d)
        s = {x["direction_code"]: x["result_status"] for x in out["directions"]}
        assert s["stress"] == "abnormal" and s["gastrointestinal"] == "abnormal"
        assert s["mobility"] == "abnormal" and s["urinary"] == "insufficient_data" and s["inflammation"] == "abnormal"
        assert db.scalar(select(func.count()).select_from(HealthDirectionRule)) == 5
        assert db.scalar(select(func.count()).select_from(HealthAttention)) == 5
        assert db.scalar(select(func.count()).select_from(MetricAnomaly)) >= 2
        assert db.scalar(select(func.count()).select_from(MetricBaseline)) >= 5
        analyze(db, "x", d)  # 幂等：重复分析不叠加
        assert db.scalar(select(func.count()).select_from(HealthAttention)) == 5


def test_no_data_is_not_normal():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        out = analyze(db, "missing", date(2026, 10, 23))
        assert all(x["result_status"] == "insufficient_data" for x in out["directions"])


def test_display_attention_has_five_neutral_placeholders(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(health_service, "_engine", engine)
    monkeypatch.setattr(health_service, "_SessionLocal", sessionmaker(bind=engine))

    out = health_service.display_attention("pet-a")

    assert out["data_status"] == "awaiting_data"
    assert len(out["directions"]) == 5
    assert all(x["result_status"] == "no_alert" for x in out["directions"])
    assert "不代表" in out["notice"]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(HealthDirectionRule)) == 5


def test_cross_source_conflict_is_excluded_from_analysis():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        target = date(2026, 10, 23)
        for n in range(1, 8):
            _put(db, "conflict", "food_frequency", 5, target - timedelta(days=n))
        _put(db, "conflict", "food_frequency", 2, target)
        db.add(
            MetricRecord(
                pet_id="conflict", metric_code="food_frequency", metric_value=9,
                unit=METRICS["food_frequency"][1],
                record_time=datetime.combine(target, datetime.min.time()),
                data_source="另一设备", period_type="daily", data_quality="valid",
                attribution_status="confirmed",
            )
        )
        db.commit()

        out = analyze(db, "conflict", target)
        assert out["data_conflicts"][0]["metric_code"] == "food_frequency"
        stress = next(x for x in out["directions"] if x["direction_code"] == "stress")
        assert stress["result_status"] == "insufficient_data"
        assert all(item["metric_code"] != "food_frequency" for item in stress["evidence"])


def test_petkit_device_cannot_be_silently_rebound():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        now = datetime(2026, 10, 23, 12)
        first = _upsert_petkit_device(db, "pet-a", "388886", "Feeder", "喂食器", now)
        db.commit()
        assert db.get(PetDevice, first.id).pet_id == "pet-a"
        with pytest.raises(ValueError, match="不能在采集时自动改绑"):
            _upsert_petkit_device(db, "pet-b", "388886", "Feeder", "喂食器", now)


def test_live_collection_rejects_historical_date_before_fetch():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        with pytest.raises(ValueError, match="实时设备采集只能写入当天"):
            sync_petkit_daily_metrics(db, "pet-a", "388886", date(2026, 1, 1))


def test_petkit_bridge_keeps_raw_data_unmapped(monkeypatch, tmp_path):
    from services import petkit_service

    export = tmp_path / "snapshot.json"
    export.write_text(
        '{"data":{"device_class":"Feeder","name":"测试喂食器",'
        '"state":{"feed_state":{"eat_count":14,"real_amount_total1":3,"real_amount_total2":2}}}}',
        encoding="utf-8",
    )
    snapshot_db = tmp_path / "raw.sqlite3"
    monkeypatch.setattr(petkit_service, "DB_PATH", snapshot_db)
    monkeypatch.setattr(
        petkit_service,
        "collect",
        lambda **_kwargs: {
            "ok": True,
            "snapshots": [{
                "export_json": str(export), "snapshot_id": 9,
                "device_class": "Feeder", "payload_sha256": "a" * 64,
                "parser_name": "test-parser", "parser_version": "1",
            }],
        },
    )
    db_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(db_engine)
    with Session(db_engine) as db:
        result = sync_petkit_daily_metrics(
            db, "pet-a", "388886", datetime.now(CN_TZ).date()
        )
        assert result["mapping_status"] == "pending"
        assert result["synced_metrics"] == {}
        assert db.scalar(select(func.count()).select_from(PetDevice)) == 1
        assert db.scalar(select(func.count()).select_from(MetricRecord)) == 0
