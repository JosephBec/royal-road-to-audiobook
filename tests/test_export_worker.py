"""Integration test of _run_job with fake TTS and fake scraping — no GPU,
no network, no real ffmpeg encode (assemble_m4b is monkeypatched)."""
import asyncio
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture()
def job_env(tmp_path, monkeypatch):
    import database, export_worker, tts
    database.init_db()
    db = database.SessionLocal()

    novel = database.Novel(title="Job Novel", author="A",
                           rr_url="https://www.royalroad.com/fiction/777/job")
    db.add(novel); db.commit()
    chapters = []
    for i in range(1, 4):
        ch = database.Chapter(novel_id=novel.id, title=f"C{i}", order=i, text=f"body {i}",
                              rr_url=f"https://www.royalroad.com/fiction/777/job/chapter/{i}/c")
        db.add(ch); chapters.append(ch)
    db.commit()

    job = database.ExportJob(novel_id=novel.id, novel_title=novel.title, author="A",
                             start_order=1, end_order=3, voice="af_heart", speed=1.0,
                             chapters_total=3)
    db.add(job); db.commit()

    monkeypatch.setattr(export_worker, "EXPORT_DIR", tmp_path / "jobs")
    monkeypatch.setattr(export_worker, "_export_may_proceed", lambda _db: True)

    async def fake_batch(text, voice, speed):
        return [np.zeros(2400, dtype=np.float32)]
    monkeypatch.setattr(tts, "synthesize_batch", fake_batch)

    assembled = {}
    def fake_assemble(chapter_wavs, out_path, **kw):
        assembled["chapters"] = [t for t, _ in chapter_wavs]
        assembled["kwargs"] = kw
        Path(out_path).write_bytes(b"m4b")
        return Path(out_path)
    import m4b
    monkeypatch.setattr(m4b, "assemble_m4b", fake_assemble)

    settings = db.query(database.Settings).first()
    orig_audiobook_dir, orig_plex_url = settings.audiobook_dir, settings.plex_url
    settings.audiobook_dir = str(tmp_path / "plexlib")
    settings.plex_url = ""  # not configured: refresh skipped with note
    db.commit()

    yield db, database, export_worker, job, assembled
    # Settings is a shared singleton row — restore it so later tests in the
    # same session (which reuses one DB) see the original defaults.
    settings = db.query(database.Settings).first()
    settings.audiobook_dir, settings.plex_url = orig_audiobook_dir, orig_plex_url
    db.query(database.ExportJob).delete()
    db.query(database.Chapter).delete()
    db.query(database.Progress).delete()
    db.query(database.Novel).delete()
    db.commit(); db.close()


def test_run_job_produces_named_m4b(job_env, tmp_path):
    db, database, export_worker, job, assembled = job_env
    asyncio.run(export_worker._run_job(job.id))

    db.expire_all()
    fresh = db.query(database.ExportJob).filter_by(id=job.id).first()
    assert fresh.status == "completed"
    assert fresh.chapters_done == 3
    out = Path(fresh.output_path)
    assert out.name == "Job Novel - Chapters 1 - 3.m4b"
    assert out.exists()
    assert assembled["chapters"] == ["C1", "C2", "C3"]
    assert assembled["kwargs"]["book_title"] == "Job Novel - Chapters 1 - 3"
    assert assembled["kwargs"]["author"] == "A"
    assert not (export_worker.EXPORT_DIR / str(job.id)).exists()  # cleaned on success


def test_retry_skips_existing_chapter_wavs(job_env, monkeypatch):
    db, database, export_worker, job, assembled = job_env
    import tts
    calls = {"n": 0}

    async def counting_batch(text, voice, speed):
        calls["n"] += 1
        import numpy as np
        return [np.zeros(2400, dtype=np.float32)]
    monkeypatch.setattr(tts, "synthesize_batch", counting_batch)

    job_dir = export_worker.EXPORT_DIR / str(job.id)
    job_dir.mkdir(parents=True)
    import soundfile as sf
    import numpy as np
    sf.write(str(job_dir / "chapter_00001.wav"),
             np.zeros(2400, dtype=np.float32), 24000, subtype="PCM_16")

    asyncio.run(export_worker._run_job(job.id))
    assert calls["n"] == 2  # chapters 2 and 3 only


def test_uncached_chapter_is_scraped_and_cached(job_env, monkeypatch):
    db, database, export_worker, job, assembled = job_env
    ch2 = (db.query(database.Chapter)
           .filter_by(novel_id=job.novel_id, order=2).first())
    ch2.text = None
    db.commit()

    class FakeScraper:
        async def scrape_chapter_text(self, url):
            return "scraped body"
    monkeypatch.setattr(export_worker, "get_scraper_for_url",
                        lambda url: FakeScraper())

    asyncio.run(export_worker._run_job(job.id))
    db.expire_all()
    fresh = db.query(database.ExportJob).filter_by(id=job.id).first()
    assert fresh.status == "completed"
    ch2 = (db.query(database.Chapter)
           .filter_by(novel_id=job.novel_id, order=2).first())
    assert ch2.text == "scraped body"  # cache populated by the worker


def test_cancel_marks_job_canceled(job_env, monkeypatch):
    db, database, export_worker, job, assembled = job_env
    import tts

    async def cancel_then_batch(text, voice, speed):
        export_worker.request_cancel(job.id)
        import numpy as np
        return [np.zeros(2400, dtype=np.float32)]
    monkeypatch.setattr(tts, "synthesize_batch", cancel_then_batch)

    asyncio.run(export_worker._run_job(job.id))
    db.expire_all()
    fresh = db.query(database.ExportJob).filter_by(id=job.id).first()
    assert fresh.status == "canceled"
    assert (export_worker.EXPORT_DIR / str(job.id)).exists()  # kept for retry
