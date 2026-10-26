"""健康关注引擎 V2 数据模型（6 张表）。

移植自 pet_health_engine_v2.zip 的 models.py，改动：
  - 默认数据库改为工程内 SQLite：var/pet_health/pet_health.db（与 cat_food_tasks.sqlite3 同惯例）；
  - 默认复用工程 MySQL 主库；可用 PET_HEALTH_DATABASE_URL 显式指定其他数据库（含 SQLite）；
  - 模块改名 pet_health_models，避免通用名 models 与工程其他模块冲突。
"""

import os
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_URL = f"sqlite:///{BASE_DIR / 'var' / 'pet_health' / 'pet_health.db'}"

Base = declarative_base()


class PetDevice(Base):
    __tablename__ = "pet_device"
    id = Column(Integer, primary_key=True)
    pet_id = Column(String(40), nullable=False, index=True)
    device_brand = Column(String(50))
    device_type = Column(String(40))
    device_uid = Column(String(100), unique=True)
    device_name = Column(String(100))
    connection_status = Column(String(20))
    last_sync_time = Column(DateTime)
    attribution_status = Column(String(20), default="unknown")


class MetricRecord(Base):
    __tablename__ = "pet_metric_record"
    id = Column(Integer, primary_key=True)
    pet_id = Column(String(40), nullable=False, index=True)
    device_id = Column(Integer, ForeignKey("pet_device.id"))
    metric_code = Column(String(50), nullable=False)
    metric_value = Column(Float, nullable=False)
    unit = Column(String(30), nullable=False)
    record_time = Column(DateTime, nullable=False)
    period_type = Column(String(20), default="daily")
    data_source = Column(String(50))
    data_quality = Column(String(20), default="valid")
    attribution_status = Column(String(20), default="confirmed")
    source_snapshot_id = Column(Integer)
    source_snapshot_db = Column(String(255))
    source_payload_sha256 = Column(String(64))
    source_field_paths = Column(JSON)
    parser_name = Column(String(100))
    parser_version = Column(String(30))
    __table_args__ = (Index("ix_metric_pet_code_time", "pet_id", "metric_code", "record_time"),)


class MetricBaseline(Base):
    __tablename__ = "pet_metric_baseline"
    id = Column(Integer, primary_key=True)
    pet_id = Column(String(40))
    metric_code = Column(String(50))
    baseline_period = Column(Integer)
    baseline_value = Column(Float)
    lower_bound = Column(Float)
    upper_bound = Column(Float)
    valid_days = Column(Integer)
    calculated_at = Column(DateTime)
    baseline_version = Column(String(30))
    target_date = Column(Date)
    __table_args__ = (
        UniqueConstraint("pet_id", "metric_code", "baseline_period", "target_date", name="uq_baseline"),
    )


class MetricAnomaly(Base):
    __tablename__ = "pet_metric_anomaly"
    id = Column(Integer, primary_key=True)
    pet_id = Column(String(40))
    metric_code = Column(String(50))
    current_value = Column(Float)
    baseline_value = Column(Float)
    change_percent = Column(Float)
    anomaly_type = Column(String(30))
    duration_days = Column(Integer, default=1)
    rule_version = Column(String(30))
    detected_at = Column(DateTime)
    target_date = Column(Date)
    __table_args__ = (
        UniqueConstraint("pet_id", "metric_code", "target_date", "rule_version", name="uq_anomaly"),
    )


class HealthDirectionRule(Base):
    __tablename__ = "health_direction_rule"
    id = Column(Integer, primary_key=True)
    direction_code = Column(String(50), unique=True)
    direction_name = Column(String(50))
    core_metrics = Column(JSON)
    auxiliary_metrics = Column(JSON)
    rule_config = Column(JSON)
    abnormal_message = Column(String(500))
    normal_message = Column(String(500))
    missing_message = Column(String(500))
    rule_version = Column(String(30))
    enabled = Column(Boolean, default=True)


class HealthAttention(Base):
    __tablename__ = "pet_health_attention"
    id = Column(Integer, primary_key=True)
    pet_id = Column(String(40), index=True)
    direction_code = Column(String(50))
    result_status = Column(String(30))
    evidence = Column(JSON)
    missing_metrics = Column(JSON)
    data_sources = Column(JSON)
    display_message = Column(String(500))
    attention_level = Column(String(20))
    rule_version = Column(String(30))
    analysis_time = Column(DateTime)
    target_date = Column(Date)
    __table_args__ = (
        UniqueConstraint("pet_id", "direction_code", "target_date", "rule_version", name="uq_attention"),
    )


def get_db_url():
    explicit = os.getenv("PET_HEALTH_DATABASE_URL")
    if explicit:
        return explicit
    from sqlalchemy.engine import URL

    from app_config import get_mysql_config

    cfg = get_mysql_config()
    return URL.create(
        "mysql+pymysql",
        username=cfg["user"],
        password=cfg["password"],
        host=cfg["host"],
        port=int(cfg["port"]),
        database=cfg["database"],
        query={"charset": cfg["charset"]},
    ).render_as_string(hide_password=False)


def make_engine(url=None):
    url = url or get_db_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    if url.startswith("sqlite"):
        Path(url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        @event.listens_for(engine, "connect")
        def _sqlite_settings(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


def migrate_schema(engine):
    """为 create_all 无法覆盖的既有库补充兼容字段。"""
    wanted = {
        "pet_device": {
            "attribution_status": "VARCHAR(20) DEFAULT 'unknown'",
        },
        "pet_metric_record": {
            "source_snapshot_id": "INTEGER",
            "source_snapshot_db": "VARCHAR(255)",
            "source_payload_sha256": "VARCHAR(64)",
            "source_field_paths": "JSON",
            "parser_name": "VARCHAR(100)",
            "parser_version": "VARCHAR(30)",
        },
    }
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table_name, columns in wanted.items():
            if table_name not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                    )


def make_session(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
