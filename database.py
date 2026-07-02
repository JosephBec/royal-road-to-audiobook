"""
Database models and session management.

SQLite database with SQLAlchemy ORM for novels, chapters, progress, and settings.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Float,
    DateTime, ForeignKey, Boolean, UniqueConstraint
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


def init_db():
    """Create all tables and ensure default settings exist."""
    Base.metadata.create_all(bind=engine)
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
