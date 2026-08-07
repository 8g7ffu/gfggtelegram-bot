"""
نماذج قاعدة البيانات لمشروع "رادار الندرة" خالية تماماً من تعارض القفل (Zero-Deadlock Architecture).
"""

import os
import time
import logging

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer,
                         String, create_engine, func)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class Collection(Base):
    __tablename__ = "collections"

    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    chain = Column(String, nullable=False, default="")
    opensea_url = Column(String, nullable=False, default="")
    total_items = Column(Integer, nullable=False, default=0)
    revealed_count = Column(Integer, nullable=False, default=0)
    computed_at = Column(DateTime(timezone=True), server_default=func.now())

    rare_items = relationship(
        "RareItem", back_populates="collection",
        cascade="all, delete-orphan", order_by="RareItem.rank"
    )


class RareItem(Base):
    __tablename__ = "rare_items"

    id = Column(Integer, primary_key=True)
    collection_id = Column(Integer, ForeignKey("collections.id"), nullable=False)
    identifier = Column(String, nullable=False)
    name = Column(String, nullable=False)
    image_url = Column(String, nullable=False, default="")
    opensea_url = Column(String, nullable=False, default="")
    rarity_score = Column(Float, nullable=False)
    rank = Column(Integer, nullable=False)
    price_eth = Column(Float, nullable=True)
    price_usd = Column(Float, nullable=True)
    tier = Column(String, nullable=True)

    collection = relationship("Collection", back_populates="rare_items")


class WatchedCollection(Base):
    __tablename__ = "watched_collections"

    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True, nullable=False, index=True)
    contract_address = Column(String, nullable=False)
    chain = Column(String, nullable=False, default="ethereum")
    max_supply = Column(Integer, nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())
    active = Column(Boolean, nullable=False, default=True)
    failed_attempts = Column(Integer, nullable=False, default=0)
    global_revealed_flag = Column(Boolean, nullable=True)
    baseline_signature = Column(String, nullable=True)
    baseline_locked = Column(Boolean, nullable=False, default=False)

    tracks = relationship("RevealTrack", back_populates="watched", cascade="all, delete-orphan")


class RevealTrack(Base):
    __tablename__ = "reveal_tracks"

    id = Column(Integer, primary_key=True)
    watched_id = Column(Integer, ForeignKey("watched_collections.id"), nullable=False)
    token_id = Column(Integer, nullable=False)
    last_uri = Column(String, nullable=True)
    content_checked_at = Column(DateTime(timezone=True), nullable=True)
    revealed = Column(Boolean, nullable=False, default=False)
    revealed_at = Column(DateTime(timezone=True), nullable=True)

    watched = relationship("WatchedCollection", back_populates="tracks")


def _normalized_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


engine = create_engine(
    _normalized_db_url(),
    pool_pre_ping=True,
    pool_recycle=120,
    pool_size=2,
    max_overflow=3,
)
SessionLocal = sessionmaker(bind=engine)


def init_db():
    """إنشاء الجداول بأعمدتها المكتملة تلقائياً بدون نداءات القفل ALTER TABLE المسببة للـ Deadlock."""
    for attempt in range(1, 6):
        try:
            Base.metadata.create_all(engine)
            break
        except Exception as e:
            logging.warning(f"[DB Init Attempt {attempt}/5] انتظار جاهزية خادم Postgres: {e}")
            time.sleep(3)
