"""
Finding words the narrator will mispronounce, before you hear it happen.

Kokoro runs a real grapheme-to-phoneme stage and emits a marker for words it
has no pronunciation for. That makes it a detector: run a chapter's vocabulary
through it and whatever comes back unknown is very likely a proper noun, an
invented term, or a coinage — exactly the words a fantasy novel is full of and
exactly the ones a TTS model will guess at.

Chatterbox has no phoneme input, so a fix is a respelling rather than IPA. The
detection still works: Kokoro finds the word, you supply a spelling that
sounds right, and it is applied as a literal text rule at render time.

The G2P stage is CPU-only and needs no GPU, so scanning can run while
Chatterbox is busy rendering.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

UNKNOWN_MARKER = "❓"
# Words worth reporting: letters, possibly hyphenated or apostrophised.
# Pure numbers and punctuation are not pronunciation problems.
# The curly apostrophe matters: without it "couldn't" splits into "couldn"
# and "t", and the fragment is reported as an unpronounceable word.
_WORD = re.compile("[A-Za-z][A-Za-z’'\\-]{2,}")

_g2p = None


def _get_g2p():
    """Kokoro's G2P, loaded once. Import is deferred: this module is imported
    by the API, which must work on a machine with no TTS stack installed."""
    global _g2p
    if _g2p is None:
        from misaki import en
        _g2p = en.G2P(trf=False, british=False, fallback=None)
    return _g2p


def available() -> tuple[bool, str]:
    try:
        import misaki  # noqa: F401
    except ImportError:
        return False, "misaki (Kokoro's G2P) is not installed"
    return True, ""


def is_unknown(word: str) -> bool:
    """True if the G2P has no pronunciation for this word."""
    try:
        phonemes, _ = _get_g2p()(word)
    except Exception:
        return False
    return UNKNOWN_MARKER in (phonemes or "")


def scan_text(text: str, skip: set[str] | None = None,
              checked: dict[str, bool] | None = None) -> dict[str, dict]:
    """Unknown words in one passage, with a count and one example sentence.

    Keyed by the lowercased word so "Gareth" and "gareth" are one entry — a
    respelling applies case-insensitively anyway.

    `checked` is a G2P result cache the caller can share across chapters. A
    novel reuses its vocabulary heavily, so without it a range scan spends
    most of its time re-deciding the same words.
    """
    skip = {s.casefold() for s in (skip or set())}
    found: dict[str, dict] = {}
    if not text:
        return found

    sentences = re.split(r"(?<=[.!?])\s+", text)
    if checked is None:
        checked = {}
    for sentence in sentences:
        for match in _WORD.finditer(sentence):
            word = match.group(0)
            key = word.casefold()
            if key in skip:
                continue
            if key not in checked:
                checked[key] = is_unknown(word)
            if not checked[key]:
                continue
            entry = found.get(key)
            if entry is None:
                found[key] = {
                    "word": word,
                    "count": 1,
                    # One short example so you can tell a name from a typo.
                    "example": sentence.strip()[:160],
                }
            else:
                entry["count"] += 1
    return found


def merge(into: dict[str, dict], other: dict[str, dict]):
    for key, entry in other.items():
        existing = into.get(key)
        if existing is None:
            into[key] = entry
        else:
            existing["count"] += entry["count"]


def scan_chapters(chapters, skip: set[str] | None = None) -> list[dict]:
    """Scan several chapters, returning findings sorted by how often each
    word appears — the ones worth fixing first are the ones said most."""
    totals: dict[str, dict] = {}
    checked: dict[str, bool] = {}   # shared across chapters: vocabulary repeats
    for chapter in chapters:
        merge(totals, scan_text(chapter.text or "", skip, checked))
    return sorted(totals.values(), key=lambda e: (-e["count"], e["word"].casefold()))
