"""
Database models and session management.

SQLite database with SQLAlchemy ORM for novels, chapters, progress, and settings.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Float,
    DateTime, ForeignKey, Boolean, UniqueConstraint,
    text, inspect as sa_inspect,
)
from sqlalchemy.orm import (
    DeclarativeBase, Session, sessionmaker, relationship
)


DATABASE_URL = "sqlite:///./data.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class Novel(Base):
    __tablename__ = "novels"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(Text, nullable=False)
    author = Column(Text, default="Unknown")
    rr_url = Column(Text, unique=True, nullable=False)
    cover_url = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    total_chapters = Column(Integer, default=0)
    last_refreshed = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Per-novel setting overrides; NULL = use global Settings default
    voice = Column(String, nullable=True)
    speed = Column(Float, nullable=True)
    auto_play = Column(Boolean, nullable=True)
    chapter_sort = Column(String, nullable=True)

    # Library organization
    favorite = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=True)  # manual card order; NULL = unordered

    chapters = relationship("Chapter", back_populates="novel", cascade="all, delete-orphan")
    progress = relationship("Progress", back_populates="novel", uselist=False, cascade="all, delete-orphan")


class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    rr_chapter_id = Column(Text, nullable=True)
    title = Column(Text, nullable=False)
    order = Column(Integer, nullable=False)
    rr_url = Column(Text, nullable=False)
    word_count = Column(Integer, default=0)
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, nullable=True)

    novel = relationship("Novel", back_populates="chapters")

    __table_args__ = (
        UniqueConstraint("novel_id", "rr_url", name="uq_novel_chapter_url"),
    )


class Progress(Base):
    __tablename__ = "progress"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), unique=True, nullable=False)
    chapter_id = Column(Integer, ForeignKey("chapters.id"), nullable=True)
    position_seconds = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    novel = relationship("Novel", back_populates="progress")
    chapter = relationship("Chapter")


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    voice = Column(String, default="af_heart")
    speed = Column(Float, default=1.0)
    playback_mode = Column(String, default="full")  # "full" or "instant"
    auto_play = Column(Boolean, default=True)
    theme = Column(String, default="dark")  # "dark" or "light"
    chapter_sort = Column(String, default="asc")  # "asc" or "desc"


def _migrate_schema():
    """Add columns introduced after initial release (SQLite has no Alembic here)."""
    inspector = sa_inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("novels")}
    new_columns = {
        "voice": "TEXT",
        "speed": "FLOAT",
        "auto_play": "BOOLEAN",
        "chapter_sort": "TEXT",
        "favorite": "BOOLEAN NOT NULL DEFAULT 0",
        "sort_order": "INTEGER",
    }
    with engine.begin() as conn:
        for name, ddl_type in new_columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE novels ADD COLUMN {name} {ddl_type}"))


def effective_settings(novel: "Novel", settings: "Settings") -> dict:
    """Resolve per-novel overrides against global settings (None = inherit)."""
    return {
        "voice": novel.voice if novel.voice is not None else (settings.voice if settings else "af_heart"),
        "speed": novel.speed if novel.speed is not None else (settings.speed if settings else 1.0),
        "auto_play": novel.auto_play if novel.auto_play is not None else (settings.auto_play if settings else True),
        "chapter_sort": novel.chapter_sort if novel.chapter_sort is not None else (settings.chapter_sort if settings else "asc"),
    }


def retention_policy(db: Session) -> tuple[set[int], set[int]]:
    """
    Cache retention sets: (keep forever, keep while fresh).

    Forever: every novel's in-progress chapter, plus the next 3 chapters of
    favorite novels (always ready for new releases). Expiring: the next 3
    chapters of non-favorites — cached for binge sessions but deleted once
    the audio file exceeds the retention window (see tts.RETENTION_SECONDS).
    """
    forever: set[int] = set()
    expiring: set[int] = set()
    for novel in db.query(Novel).all():
        current_order = 0
        prog = db.query(Progress).filter(Progress.novel_id == novel.id).first()
        if prog and prog.chapter_id:
            forever.add(prog.chapter_id)
            ch = db.query(Chapter).filter(Chapter.id == prog.chapter_id).first()
            if ch:
                current_order = ch.order
        next_ids = [
            c.id for c in (
                db.query(Chapter)
                .filter(Chapter.novel_id == novel.id, Chapter.order > current_order)
                .order_by(Chapter.order)
                .limit(3)
                .all()
            )
        ]
        (forever if novel.favorite else expiring).update(next_ids)
    return forever, expiring


def init_db():
    """Create all tables and ensure default settings exist."""
    Base.metadata.create_all(bind=engine)
    _migrate_schema()
    db = SessionLocal()
    try:
        settings = db.query(Settings).first()
        if not settings:
            db.add(Settings(voice="af_heart", speed=1.0, playback_mode="full", auto_play=True, theme="dark", chapter_sort="asc"))
            db.commit()
        else:
            # Migrate old playback_mode values
            if settings.playback_mode not in ("full", "instant"):
                settings.playback_mode = "full"
                db.commit()
    finally:
        db.close()


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
