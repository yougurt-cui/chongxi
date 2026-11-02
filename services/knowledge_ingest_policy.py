"""Explicit, auditable boundary between skipped legacy data and ID-linked ingestion."""
from datetime import datetime, timezone
from sqlalchemy import BigInteger, Column, DateTime, String, inspect
from sqlalchemy.orm import Session, declarative_base

Base = declarative_base()


class IngestPolicy(Base):
    __tablename__ = 'knowledge_ingest_policy'
    name = Column(String(60), primary_key=True)
    post_floor = Column(BigInteger, nullable=False)
    comment_floor = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    status = Column(String(30), nullable=False, default='legacy_skipped')


def load(db):
    if not inspect(db.connection()).has_table(IngestPolicy.__tablename__):
        return None
    row = db.get(IngestPolicy, 'douyin')
    return {'post_floor': int(row.post_floor), 'comment_floor': int(row.comment_floor)} if row else None


def for_engine(engine):
    with Session(engine) as db:
        return load(db)
