"""
Database models and session management.

SQLite database with SQLAlchemy ORM for novels, chapters, progress, and settings.
"""

import logging
import os
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


logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("NOVEL_TTS_DB", "sqlite:///./data.db")

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

    # Render dialogue in per-character voices using this novel's voice scripts.
    # Off by default: single-voice playback stays exactly as it is until you
    # opt a novel in.
    multi_voice = Column(Boolean, nullable=False, default=False)

    # Per-novel setting overrides; NULL = use global Settings default
    engine = Column(String, nullable=True)
    voice = Column(String, nullable=True)
    speed = Column(Float, nullable=True)
    auto_play = Column(Boolean, nullable=True)
    chapter_sort = Column(String, nullable=True)

    # Library organization
    favorite = Column(Boolean, nullable=False, default=False)
    # Finished, or on a break. Hidden from the library, excluded from the
    # favorites crawl, prefetch and head start, and its audio is no longer
    # kept forever — an abandoned novel shouldn't hold a chapter of audio
    # indefinitely just because it was once started.
    archived = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=True)  # manual card order; NULL = unordered

    chapters = relationship("Chapter", back_populates="novel", cascade="all, delete-orphan")
    progress = relationship("Progress", back_populates="novel", uselist=False, cascade="all, delete-orphan")


class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    rr_chapter_id = Column(Text, nullable=True)
    title = Column(Text, nullable=False)
    order = Column(Integer, nullable=False)  # position in playback order
    # The author's own chapter number where the source states one (EPUB TOC).
    # Separate from `order`: front matter shifts position but isn't numbered.
    chapter_number = Column(Integer, nullable=True)
    rr_url = Column(Text, nullable=False)
    word_count = Column(Integer, default=0)
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, nullable=True)
    text = Column(Text, nullable=True)  # scraped chapter text cache (scrape once, ever)

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


class Character(Base):
    """A speaking character in one novel, optionally cast to a voice.

    Scoped per novel rather than globally: two books can have a Gareth without
    them being the same person. Two characters sharing a name *within* one book
    is rare enough that it is deliberately not modelled — if it ever bites, the
    fix is an alias, not a schema change.
    """
    __tablename__ = "characters"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    name = Column(Text, nullable=False)
    # Other names the text uses for them ("the captain", a surname, a title).
    aliases = Column(Text, nullable=True)          # JSON list
    description = Column(Text, nullable=True)      # for casting a voice by feel
    voice = Column(String, nullable=True)          # NULL = fall back to narrator
    engine = Column(String, nullable=True)         # engine that voice belongs to
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    novel = relationship("Novel")

    __table_args__ = (
        UniqueConstraint("novel_id", "name", name="uq_novel_character_name"),
    )


class ChapterScript(Base):
    """Per-chapter map of which span of text is spoken by whom.

    `spans` is JSON rather than rows: it is read and written whole, always in
    order, and never queried by field. One row per chapter keeps re-tagging a
    single atomic write.

    `text_hash` pins the script to the exact chapter text it was built from —
    a re-scrape that changes wording invalidates the span indices, and a stale
    script would assign lines to the wrong speaker.
    """
    __tablename__ = "chapter_scripts"

    chapter_id = Column(Integer, ForeignKey("chapters.id"), primary_key=True)
    text_hash = Column(String, nullable=False)
    spans = Column(Text, nullable=False)           # JSON list
    source = Column(String, nullable=False, default="rule")  # rule | external
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    chapter = relationship("Chapter")


class TextRule(Base):
    """A find-and-replace applied to chapter text before it is spoken.

    Progression fiction is full of notation that reads fine on a page and
    badly aloud: "Stealth V", or a skill name with an arrow between two
    numbers. The arrow carries the meaning visually and vanishes entirely in
    speech. Rules restore that meaning without editing the source text.

    Scoped per novel because the vocabulary is: one book's "V" is a tier,
    another's is a name. novel_id NULL means it applies everywhere.
    """
    __tablename__ = "text_rules"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=True)
    kind = Column(String, nullable=False, default="regex")  # regex | roman
    pattern = Column(Text, nullable=False)
    replacement = Column(Text, nullable=False, default="")
    note = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    novel = relationship("Novel")


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    engine = Column(String, default="kokoro")  # see engines/ registry
    voice = Column(String, default="af_heart")
    speed = Column(Float, default=1.0)
    playback_mode = Column(String, default="full")  # "full" or "instant"
    auto_play = Column(Boolean, default=True)
    theme = Column(String, default="dark")  # "dark", "light", "oled", "warm"
    accent = Column(String, nullable=True)  # "#rrggbb"; NULL = theme's default
    chapter_sort = Column(String, default="asc")  # "asc" or "desc"
    audiobook_dir = Column(Text, nullable=False, default=r"E:\Plex\Audiobooks\Audiobooks")
    plex_url = Column(Text, nullable=False, default="")
    plex_token = Column(Text, nullable=False, default="")
    plex_section_id = Column(Text, nullable=False, default="")


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    novel_title = Column(Text, nullable=False)   # snapshot: job survives novel edits
    author = Column(Text, default="Unknown")
    start_order = Column(Integer, nullable=False)
    end_order = Column(Integer, nullable=False)
    engine = Column(String, nullable=True)  # snapshot: job renders with the engine chosen at queue time
    voice = Column(String, nullable=False)
    speed = Column(Float, nullable=False)
    status = Column(String, nullable=False, default="queued")
    chapters_done = Column(Integer, default=0)
    chapters_total = Column(Integer, default=0)
    detail = Column(Text, default="")
    output_path = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)


def _migrate_schema():
    """Add columns introduced after initial release (SQLite has no Alembic here)."""
    inspector = sa_inspect(engine)
    table_columns = {
        "novels": {
            "multi_voice": "BOOLEAN NOT NULL DEFAULT 0",
            "engine": "TEXT",
            "voice": "TEXT",
            "speed": "FLOAT",
            "auto_play": "BOOLEAN",
            "chapter_sort": "TEXT",
            "favorite": "BOOLEAN NOT NULL DEFAULT 0",
            "archived": "BOOLEAN NOT NULL DEFAULT 0",
            "sort_order": "INTEGER",
        },
        "chapters": {
            "text": "TEXT",
            "chapter_number": "INTEGER",
        },
        "export_jobs": {
            "engine": "TEXT",
        },
        "settings": {
            "engine": "TEXT NOT NULL DEFAULT 'kokoro'",
            "audiobook_dir": "TEXT NOT NULL DEFAULT 'E:\\Plex\\Audiobooks\\Audiobooks'",
            "plex_url": "TEXT NOT NULL DEFAULT ''",
            "plex_token": "TEXT NOT NULL DEFAULT ''",
            "plex_section_id": "TEXT NOT NULL DEFAULT ''",
            "accent": "TEXT",
        },
    }
    with engine.begin() as conn:
        for table, new_columns in table_columns.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl_type in new_columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))


def effective_settings(novel: "Novel", settings: "Settings") -> dict:
    """Resolve per-novel overrides against global settings (None = inherit).

    The voice is additionally coerced to one the resolved engine actually
    provides — switching engines otherwise leaves a voice id the new engine
    has never heard of.
    """
    import engines

    engine = novel.engine if novel.engine is not None else (
        (getattr(settings, "engine", None) or "kokoro") if settings else "kokoro")
    voice = novel.voice if novel.voice is not None else (settings.voice if settings else "af_heart")
    return {
        "engine": engine,
        "voice": engines.resolve_voice(engine, voice),
        "speed": novel.speed if novel.speed is not None else (settings.speed if settings else 1.0),
        "auto_play": novel.auto_play if novel.auto_play is not None else (settings.auto_play if settings else True),
        "chapter_sort": novel.chapter_sort if novel.chapter_sort is not None else (settings.chapter_sort if settings else "asc"),
    }


def ensure_progress(db: Session, novel: "Novel") -> Optional["Progress"]:
    """Point a novel at its first chapter from the moment it is imported.

    A missing Progress row used to mean "never opened", and every consumer had
    to special-case it: the cache sweep guessed at the first chapter, the
    library card showed no position, and the chapter list drew no current
    marker. The guess was the same one everywhere, so the absent row carried no
    information — it only made four callers reimplement one fallback. Creating
    the row up front deletes the case instead of handling it.

    `updated_at` stays NULL, and that is what still separates a book you have
    never played from one you are reading. Both the priority ordering and the
    export gate read it, and a synthetic "now" would make a book imported an
    hour ago look like the one you were listening to a second ago.

    Idempotent, and the caller commits.
    """
    progress = db.query(Progress).filter(Progress.novel_id == novel.id).first()
    if progress is not None and progress.chapter_id is not None:
        return progress

    first = (db.query(Chapter).filter(Chapter.novel_id == novel.id)
             .order_by(Chapter.order).first())
    if first is None:
        return progress          # no chapters yet; nothing to point at

    if progress is None:
        progress = Progress(novel_id=novel.id, chapter_id=first.id,
                            position_seconds=0.0)
        db.add(progress)
        db.flush()               # the column default lands on the INSERT...
    else:
        progress.chapter_id = first.id
    progress.updated_at = None   # ...and only an UPDATE can clear it again
    return progress


def init_db():
    """Create all tables and ensure default settings exist."""
    Base.metadata.create_all(bind=engine)
    _migrate_schema()
    db = SessionLocal()
    try:
        settings = db.query(Settings).first()
        if not settings:
            db.add(Settings(engine="kokoro", voice="af_heart", speed=1.0, playback_mode="full",
                            auto_play=True, theme="dark", chapter_sort="asc"))
            db.commit()
        else:
            dirty = False
            # Migrate old playback_mode values
            if settings.playback_mode not in ("full", "instant"):
                settings.playback_mode = "full"
                dirty = True
            # Rows created before the engine column existed
            if not settings.engine:
                settings.engine = "kokoro"
                dirty = True
            if dirty:
                db.commit()

        # Every novel points at a chapter (see ensure_progress). Backfills
        # libraries imported before that became an invariant, so nothing
        # downstream has to keep handling the absent row.
        pointed = 0
        for novel in db.query(Novel).all():
            existing = db.query(Progress).filter(Progress.novel_id == novel.id).first()
            if existing is not None and existing.chapter_id is not None:
                continue
            if ensure_progress(db, novel) is not None:
                pointed += 1
        if pointed:
            db.commit()
            logger.info("Pointed %d novel(s) at their first chapter", pointed)

        # Heal libraries damaged by the old URL-keyed chapter dedup (see
        # chapter_repair). No-op once clean, so it is safe on every startup.
        import chapter_repair
        reports = chapter_repair.repair_all(db, Chapter, Novel, Progress)
        if reports:
            stale = chapter_repair.orphaned_audio_ids(reports)
            try:
                from tts import remove_chapter_audio
                remove_chapter_audio(stale)
            except Exception:  # audio cleanup must never block startup
                pass
    finally:
        db.close()


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
