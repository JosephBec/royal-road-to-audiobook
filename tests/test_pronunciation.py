"""Finding words the narrator has no pronunciation for.

Kokoro's G2P emits a marker for words it can't pronounce, which makes it a
detector for invented vocabulary. The fix is a respelling, applied as a
literal text rule — Chatterbox has no phoneme input, so the sound is the only
thing that can be corrected.
"""
import pytest

import pronunciation as pr
import text_rules as tr


def _skip_without_g2p():
    ok, reason = pr.available()
    if not ok:
        pytest.skip(reason)


# ----- literal rules are what a respelling is -----

def test_respelling_replaces_every_case():
    out = tr.apply_rule("Gareth spoke. gareth waited.", "literal", "Gareth", "GARR-eth")
    assert out == "GARR-eth spoke. GARR-eth waited."


def test_respelling_respects_word_boundaries():
    """Replacing inside a longer word would corrupt unrelated text."""
    assert tr.apply_rule("Ulimar and Ulima", "literal", "Ulima", "oo-LEE-ma") \
        == "Ulimar and oo-LEE-ma"


def test_respelling_escapes_regex_characters():
    """A name is not a pattern; '.' must not match any character."""
    assert tr.apply_rule("Dr. Who and Drs", "literal", "Dr.", "Doctor") == "Doctor Who and Drs"


def test_respelling_requires_a_replacement():
    assert tr.validate("literal", "Gareth", "") is not None
    assert tr.validate("literal", "Gareth", "GARR-eth") is None


def test_literal_preview_reports_matches():
    count, examples = tr.preview("Gareth met Gareth.", "literal", "Gareth", "GARR-eth")
    assert count == 2
    assert examples[0]["after"] == "GARR-eth"


# ----- detection -----

def test_invented_words_are_flagged():
    _skip_without_g2p()
    assert pr.is_unknown("Caelynthir")
    assert pr.is_unknown("Aethersensing")


def test_ordinary_words_are_not_flagged():
    _skip_without_g2p()
    assert not pr.is_unknown("running")
    assert not pr.is_unknown("mountain")


def test_scan_counts_occurrences_and_keeps_an_example():
    _skip_without_g2p()
    text = ("Caelynthir drew his blade. The road was long. "
            "Caelynthir did not look back.")
    found = pr.scan_text(text)
    assert "caelynthir" in found
    assert found["caelynthir"]["count"] == 2
    assert "Caelynthir" in found["caelynthir"]["example"]


def test_scan_skips_words_already_respelled():
    """A word you have fixed should not reappear on every rescan."""
    _skip_without_g2p()
    text = "Caelynthir drew his blade."
    assert pr.scan_text(text, skip={"Caelynthir"}) == {}


def test_contractions_are_not_reported_as_fragments():
    """Splitting on a curly apostrophe reported 'couldn' as a broken word."""
    _skip_without_g2p()
    found = pr.scan_text("He couldn’t reach it. She wasn’t sure.")
    assert "couldn" not in found
    assert "wasn" not in found


def test_pure_numbers_are_not_pronunciation_problems():
    _skip_without_g2p()
    assert pr.scan_text("There were 42 of them, and 1999 more.") == {}


class FakeChapter:
    def __init__(self, text):
        self.text = text


def test_scan_across_chapters_sorts_by_frequency():
    _skip_without_g2p()
    chapters = [
        FakeChapter("Caelynthir spoke. Caelynthir waited."),
        FakeChapter("Caelynthir left. Xylophia arrived."),
    ]
    results = pr.scan_chapters(chapters)
    assert results[0]["word"].casefold() == "caelynthir"
    assert results[0]["count"] == 3
