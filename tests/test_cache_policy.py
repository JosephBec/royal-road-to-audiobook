"""What gets cached, in what order, and what survives retention.

No GPU and no network: the policy is pure database reads plus "does this file
exist", so it can be exercised exactly as the worker sees it.
"""
from datetime import datetime, timedelta, timezone

import pytest


def _wipe(session, database):
    session.query(database.Progress).delete()
    session.query(database.Chapter).delete()
    session.query(database.Novel).delete()
    session.commit()


@pytest.fixture()
def db():
    import database
    database.init_db()
    session = database.SessionLocal()
    # Clean going in as well as coming out: the plan is a view over the whole
    # library, so a novel another test module left behind would show up in it.
    _wipe(session, database)
    yield session
    _wipe(session, database)
    session.close()


def _novel(db, seed, chapters=10, favorite=False, archived=False):
    import database
    novel = database.Novel(title=seed, rr_url=f"https://rr.test/{seed}",
                           favorite=favorite, archived=archived)
    db.add(novel)
    db.flush()
    rows = []
    for order in range(1, chapters + 1):
        ch = database.Chapter(novel_id=novel.id, title=f"{seed} c{order}",
                              order=order,
                              rr_url=f"https://rr.test/{seed}/{order}")
        db.add(ch)
        rows.append(ch)
    db.commit()
    return novel, rows


def _reading(db, novel, chapter, minutes_ago=1):
    """Put the reader on a chapter, having last listened `minutes_ago`."""
    import database
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.add(database.Progress(novel_id=novel.id, chapter_id=chapter.id,
                             updated_at=stamp))
    db.commit()


# ----- the window -----


def test_window_is_current_next_three_and_previous(db):
    import cache_policy
    novel, chs = _novel(db, "window")
    _reading(db, novel, chs[4])                      # chapter 5 of 10

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert set(plan) == {chs[3].id, chs[4].id, chs[5].id, chs[6].id, chs[7].id}


def test_window_is_ordered_the_way_a_listener_reaches_it(db):
    """Current, then autoplay's next, then the one you go back to, then the tail.

    Ordering is the whole product here: at Chatterbox speeds the tail of the
    window is an hour of GPU behind the front of it, so whatever sorts last is
    effectively not cached at all.
    """
    import cache_policy
    novel, chs = _novel(db, "order")
    _reading(db, novel, chs[4])

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert plan == [chs[4].id, chs[5].id, chs[3].id, chs[6].id, chs[7].id]


def test_first_chapter_has_no_previous(db):
    import cache_policy
    novel, chs = _novel(db, "first")
    _reading(db, novel, chs[0])

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert plan == [chs[0].id, chs[1].id, chs[2].id, chs[3].id]


def test_archived_novels_are_cached_not_at_all(db):
    """Archiving is how you say you are not reading a book."""
    import cache_policy
    novel, chs = _novel(db, "archived", archived=True)
    _reading(db, novel, chs[4])

    assert cache_policy.cache_plan(db) == []


def test_a_novel_with_no_chapters_is_skipped(db):
    import cache_policy
    _novel(db, "empty", chapters=0)

    assert cache_policy.cache_plan(db) == []


# ----- new releases -----


def test_a_caught_up_favorite_new_release_outranks_everything(db):
    import cache_policy
    behind, behind_chs = _novel(db, "behind")
    _reading(db, behind, behind_chs[4], minutes_ago=1)      # read most recently
    fav, fav_chs = _novel(db, "fav", favorite=True)
    _reading(db, fav, fav_chs[8], minutes_ago=90)           # chapter 9 of 10

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert plan[0] == fav_chs[9].id, "the new chapter of a caught-up favorite goes first"


def test_a_far_behind_favorite_does_not_jump_the_queue(db):
    """The real case: Path to Transcendence, 21 chapters behind.

    Its newest chapter is weeks of listening away, so prioritising it would
    starve the chapter genuinely up next — of the same book.
    """
    import cache_policy
    fav, chs = _novel(db, "farbehind", chapters=30, favorite=True)
    _reading(db, fav, chs[4])                                # chapter 5 of 30

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert chs[29].id not in plan, "the newest chapter is not in the window at all"
    assert plan[0] == chs[4].id, "the chapter under the playhead comes first"


def test_a_non_favorite_new_release_does_not_jump_the_queue(db):
    import cache_policy
    plain, plain_chs = _novel(db, "plain")
    _reading(db, plain, plain_chs[8], minutes_ago=90)
    other, other_chs = _novel(db, "other")
    _reading(db, other, other_chs[4], minutes_ago=1)

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert plan[0] == other_chs[4].id, "recency decides; favorite-ness alone does not"


def test_a_caught_up_favorite_with_nothing_new_adds_no_release(db):
    """Caught up with no new chapter is the normal state, not a target."""
    import cache_policy
    fav, chs = _novel(db, "current", chapters=5, favorite=True)
    _reading(db, fav, chs[4])                                # the last chapter

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert plan == [chs[4].id, chs[3].id]


# ----- priority between novels -----


def test_the_book_you_are_reading_is_planned_first(db):
    import cache_policy
    old, old_chs = _novel(db, "old")
    _reading(db, old, old_chs[4], minutes_ago=600)
    fresh, fresh_chs = _novel(db, "fresh")
    _reading(db, fresh, fresh_chs[4], minutes_ago=1)

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert plan[0] == fresh_chs[4].id
    assert plan.index(fresh_chs[7].id) < plan.index(old_chs[4].id), \
        "the whole window of the active book outranks the next book's"


def test_a_never_played_novel_sorts_last(db):
    """An import writes a Progress row, but not a listening timestamp.

    Without that distinction a book added an hour ago would outrank the one you
    were listening to this morning, purely because its row is newer.
    """
    import cache_policy, database
    played, played_chs = _novel(db, "played")
    _reading(db, played, played_chs[0], minutes_ago=600)
    imported, imported_chs = _novel(db, "imported")
    database.ensure_progress(db, imported)
    db.commit()

    plan = [t.chapter_id for t in cache_policy.cache_plan(db)]

    assert plan[0] == played_chs[0].id
    assert imported_chs[0].id in plan, "it is still cached, just last"


def test_an_imported_novel_starts_on_chapter_one(db):
    import cache_policy, database
    novel, chs = _novel(db, "import")
    database.ensure_progress(db, novel)
    db.commit()

    progress = db.query(database.Progress).filter(
        database.Progress.novel_id == novel.id).first()
    assert progress.chapter_id == chs[0].id
    assert progress.updated_at is None, "never played is not the same as played now"
    assert cache_policy.cache_plan(db)[0].chapter_id == chs[0].id


def test_ensure_progress_never_moves_a_reader_back(db):
    import cache_policy, database
    novel, chs = _novel(db, "keepplace")
    _reading(db, novel, chs[6])

    database.ensure_progress(db, novel)
    db.commit()

    progress = db.query(database.Progress).filter(
        database.Progress.novel_id == novel.id).first()
    assert progress.chapter_id == chs[6].id


# ----- the target payload -----


def test_targets_carry_the_novel_s_own_voice(db):
    """Per-novel overrides have to reach the renderer, or the sweep caches a
    chapter in the wrong voice and playback re-renders it from scratch."""
    import cache_policy
    novel, chs = _novel(db, "voiced")
    novel.engine = "chatterbox"
    novel.voice = "cb_yearsley"
    _reading(db, novel, chs[0])

    target = cache_policy.cache_plan(db)[0]

    assert (target.engine, target.voice) == ("chatterbox", "cb_yearsley")
    assert target.url == chs[0].rr_url


# ----- retention -----


def test_retention_keeps_exactly_the_plan(db):
    import cache_policy
    novel, chs = _novel(db, "keep")
    _reading(db, novel, chs[4])

    keep, _ = cache_policy.retention_sets(db)

    assert keep == {t.chapter_id for t in cache_policy.cache_plan(db)}


def test_retention_gives_finished_chapters_a_grace_period(db):
    """Advancing a chapter should not destroy the one just finished.

    Those are complete renders — twenty minutes of GPU each — and the reader
    may well go back. They expire on the retention window instead.
    """
    import cache_policy
    novel, chs = _novel(db, "grace")
    _reading(db, novel, chs[6])                      # chapter 7

    keep, expiring = cache_policy.retention_sets(db)

    assert chs[5].id in keep, "the immediately previous chapter is kept outright"
    assert {chs[1].id, chs[2].id, chs[3].id, chs[4].id} <= expiring
    assert not (keep & expiring), "a chapter is in one set or the other"


def test_retention_drops_everything_beyond_the_grace_band(db):
    import cache_policy
    novel, chs = _novel(db, "far", chapters=20)
    _reading(db, novel, chs[14])                     # chapter 15

    keep, expiring = cache_policy.retention_sets(db)

    assert chs[0].id not in keep and chs[0].id not in expiring
    assert chs[19].id not in keep and chs[19].id not in expiring


def test_retention_keeps_nothing_for_an_archived_novel(db):
    import cache_policy
    novel, chs = _novel(db, "arch2", archived=True)
    _reading(db, novel, chs[4])

    keep, expiring = cache_policy.retention_sets(db)

    assert keep == set() and expiring == set()


# ----- "does this chapter have its opening" -----


def test_head_start_satisfied_by_enough_rendered_audio(db, tmp_path, monkeypatch):
    import cache_policy, tts
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)
    for index, seconds in enumerate((60.0, 60.0, 30.0)):
        tts.record_segment_duration(4242, index, seconds)

    assert cache_policy.head_start_satisfied(4242) is True


def test_a_partial_opening_is_not_an_opening(db, tmp_path, monkeypatch):
    import cache_policy, tts
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)
    tts.record_segment_duration(4243, 0, 30.0)

    assert cache_policy.head_start_satisfied(4243) is False
    assert cache_policy.head_start_satisfied(4244) is False   # nothing at all


def test_a_short_chapter_counts_once_it_is_complete(db, tmp_path, monkeypatch):
    """A foreword can be ninety seconds long and never reach the head start.

    Without this the sweep would re-enter it on every tick forever, and each
    resume drops the last segment as possibly-truncated — so it would throw
    away a segment and re-render it, indefinitely.
    """
    import cache_policy, tts
    monkeypatch.setattr(tts, "TEMP_DIR", tmp_path)
    tts.record_segment_duration(4245, 0, 90.0)
    tts.temp_path_for_chapter(4245).write_bytes(b"complete render")

    assert cache_policy.head_start_satisfied(4245) is True
