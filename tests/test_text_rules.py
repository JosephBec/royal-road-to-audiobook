"""Per-novel text rules.

Progression fiction uses notation that carries meaning visually and loses it
entirely in speech. A wrong rule is discovered by hearing it, so validation
and preview matter as much as the substitution itself.
"""
import pytest

import text_rules as tr


# ----- roman numerals -----

@pytest.mark.parametrize("token,value", [
    ("I", 1), ("IV", 4), ("V", 5), ("IX", 9), ("X", 10),
    ("XIV", 14), ("XL", 40), ("MCMXCIV", 1994),
])
def test_roman_values(token, value):
    assert tr.roman_to_int(token) == value


@pytest.mark.parametrize("token", ["", "banana", "IIII I", "5", "VX2"])
def test_non_roman_rejected(token):
    assert tr.roman_to_int(token) is None


def test_a_lone_i_is_still_roman():
    """Common in tier notation; also the English pronoun, hence per-novel rules."""
    assert tr.roman_to_int("I") == 1


# ----- the two motivating cases -----

def test_tier_numerals_become_numbers():
    out = tr.apply_rule("Stealth V and Perception III were his best.",
                        "roman", r"\b([A-Z][a-z]+) ([IVXL]+)\b", r"\1 tier \2")
    assert out == "Stealth tier 5 and Perception tier 3 were his best."


def test_level_up_arrow_becomes_words():
    """The arrow is the whole meaning and is silent when narrated."""
    out = tr.apply_rule("Blade Mastery 4 -> 5 after the fight.", "regex",
                        r"([A-Za-z][A-Za-z ]+?) (\d+)\s*(?:->|→)\s*(\d+)",
                        r"\1 increased from level \2 to level \3")
    assert out == "Blade Mastery increased from level 4 to level 5 after the fight."


def test_unicode_arrow_matches_too():
    out = tr.apply_rule("Toughness 2 → 3.", "regex",
                        r"(\w+) (\d+)\s*→\s*(\d+)", r"\1 from \2 to \3")
    assert out == "Toughness from 2 to 3."


def test_regex_kind_leaves_numerals_alone():
    """Only the roman kind converts; otherwise 'Chapter V' would change."""
    out = tr.apply_rule("Stealth V", "regex", r"(\w+) ([IVXL]+)", r"\1 tier \2")
    assert out == "Stealth tier V"


# ----- validation: a bad rule must be refused, not discovered by ear -----

def test_invalid_regex_is_reported():
    assert "invalid regular expression" in tr.validate("regex", "([unclosed", "x")


def test_empty_matching_pattern_is_refused():
    """A pattern matching the empty string substitutes between every character."""
    assert "empty string" in tr.validate("regex", "a*", "x")


def test_backreference_beyond_group_count_is_refused():
    assert "\\2" in tr.validate("regex", r"(\d)", r"\2")


def test_unknown_kind_is_refused():
    assert "unknown rule kind" in tr.validate("banana", "x", "y")


def test_overlong_pattern_is_refused():
    assert "longer than" in tr.validate("regex", "a" * 600, "x")


def test_valid_rule_returns_no_complaint():
    assert tr.validate("roman", r"(\w+) ([IVX]+)", r"\1 tier \2") is None


# ----- a broken rule must not break a chapter -----

def test_a_failing_rule_leaves_text_untouched():
    original = "Nothing should happen to this."
    assert tr.apply_rule(original, "regex", "([unclosed", "x") == original


class FakeRule:
    def __init__(self, pattern, replacement, kind="regex", enabled=True):
        self.pattern, self.replacement = pattern, replacement
        self.kind, self.enabled = kind, enabled


def test_rules_apply_in_order():
    rules = [FakeRule("cat", "dog"), FakeRule("dog", "bird")]
    assert tr.apply_rules("cat", rules) == "bird"


def test_disabled_rules_are_skipped():
    assert tr.apply_rules("cat", [FakeRule("cat", "dog", enabled=False)]) == "cat"


# ----- preview -----

def test_preview_reports_matches_without_changing_anything():
    text = "Stealth V, Perception III, Endurance II"
    count, examples = tr.preview(text, "roman", r"([A-Z][a-z]+) ([IVXL]+)", r"\1 tier \2")
    assert count == 3
    assert examples[0] == {"before": "Stealth V", "after": "Stealth tier 5"}


def test_preview_refuses_an_invalid_pattern():
    with pytest.raises(ValueError):
        tr.preview("text", "regex", "([unclosed", "x")


def test_preview_caps_the_examples_returned():
    text = " ".join(f"Skill{i} V" for i in range(30))
    count, examples = tr.preview(text, "roman", r"(\w+) ([IVXL]+)", r"\1 tier \2")
    assert count == 30
    assert len(examples) <= 8


# ----- text backfill -----

def test_backfill_targets_only_chapters_without_text():
    """Refetching a chapter that already has text wastes a request on someone
    else's server for no benefit."""
    import database, text_backfill
    database.init_db()
    db = database.SessionLocal()
    novel = database.Novel(title="Backfill Test",
                           rr_url="https://example.com/fiction/8888/b")
    db.add(novel); db.flush()
    for order in (1, 2, 3):
        db.add(database.Chapter(
            novel_id=novel.id, title=f"C{order}", order=order,
            rr_url=f"https://example.com/fiction/8888/b/chapter/{order}",
            text="already here" if order == 2 else None))
    db.commit()

    pending = text_backfill._pending_chapters(db, novel.id, None, None)
    assert [c.order for c in pending] == [1, 3]

    ranged = text_backfill._pending_chapters(db, novel.id, 3, None)
    assert [c.order for c in ranged] == [3]

    db.delete(db.query(database.Novel).filter(database.Novel.id == novel.id).first())
    db.commit(); db.close()


def test_backfill_finds_only_novels_actually_missing_text():
    """The refresh sweep must not re-walk books that are already complete."""
    import database, text_backfill
    database.init_db()
    db = database.SessionLocal()

    complete = database.Novel(title="Complete Novel",
                              rr_url="https://example.com/fiction/9001/a")
    partial = database.Novel(title="Partial Novel",
                             rr_url="https://example.com/fiction/9002/b")
    archived = database.Novel(title="Archived Novel", archived=True,
                              rr_url="https://example.com/fiction/9003/c")
    db.add_all([complete, partial, archived]); db.flush()
    db.add(database.Chapter(novel_id=complete.id, title="c", order=1,
                            rr_url="u1", text="here"))
    db.add(database.Chapter(novel_id=partial.id, title="c", order=1,
                            rr_url="u2", text=None))
    db.add(database.Chapter(novel_id=archived.id, title="c", order=1,
                            rr_url="u3", text=None))
    db.commit()
    partial_id, complete_id, archived_id = partial.id, complete.id, archived.id
    db.close()

    missing = text_backfill.novels_missing_text()
    assert partial_id in missing
    assert complete_id not in missing
    # Archiving a book is how you say "stop spending effort on this one".
    assert archived_id not in missing

    db = database.SessionLocal()
    for nid in (complete_id, partial_id, archived_id):
        db.delete(db.query(database.Novel).filter(database.Novel.id == nid).first())
    db.commit(); db.close()


def test_refresh_sweep_yields_to_a_user_triggered_backfill(monkeypatch):
    """Two backfills at once would double the request rate at the source."""
    import asyncio, text_backfill

    monkeypatch.setattr(text_backfill, "novels_missing_text", lambda: [1, 2, 3])
    monkeypatch.setattr(text_backfill, "is_running", lambda: True)
    ran = []

    async def fake_run(novel_id, start, end):
        ran.append(novel_id)
    monkeypatch.setattr(text_backfill, "_run", fake_run)

    asyncio.run(text_backfill.backfill_all_active())
    assert ran == []


def test_refresh_sweep_covers_every_novel_missing_text(monkeypatch):
    import asyncio, text_backfill

    monkeypatch.setattr(text_backfill, "novels_missing_text", lambda: [4, 5])
    monkeypatch.setattr(text_backfill, "is_running", lambda: False)
    ran = []

    async def fake_run(novel_id, start, end):
        ran.append(novel_id)
    monkeypatch.setattr(text_backfill, "_run", fake_run)

    asyncio.run(text_backfill.backfill_all_active())
    assert ran == [4, 5]


def test_one_failing_novel_does_not_stop_the_sweep(monkeypatch):
    """A dead link in one book must not strand the rest of the library."""
    import asyncio, text_backfill

    monkeypatch.setattr(text_backfill, "novels_missing_text", lambda: [6, 7, 8])
    monkeypatch.setattr(text_backfill, "is_running", lambda: False)
    ran = []

    async def fake_run(novel_id, start, end):
        ran.append(novel_id)
        if novel_id == 7:
            raise RuntimeError("scraper exploded")
    monkeypatch.setattr(text_backfill, "_run", fake_run)

    asyncio.run(text_backfill.backfill_all_active())
    assert ran == [6, 7, 8]


def test_backfill_refuses_to_start_while_one_is_running(monkeypatch):
    """Two concurrent backfills would double the request rate at the source."""
    import text_backfill

    class FakeTask:
        def done(self):
            return False

    monkeypatch.setattr(text_backfill, "_task", FakeTask())
    result = text_backfill.start(1, None, None)
    assert result["started"] is False
    assert "already running" in result["reason"]
