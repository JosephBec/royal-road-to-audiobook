"""
Voice scripts: which span of a chapter is spoken by whom.

A script is an ordered list of spans, one per sentence, each tagged as
narration or speech and optionally attributed to a character. Rendering reads
it to pick a voice per sentence; sentences are already the synthesis unit, so
the seam exists.

Three things can produce a span's attribution, in increasing authority:

    rule      cheap local heuristics — quote detection, explicit "X said"
    external  an LLM tagger, which will run off-box and POST its results
    manual    you, correcting a mistake

Manual always wins and survives re-tagging. That is the whole reason the
`source` field exists: attribution is never going to be perfect at any model
size, so a correction has to be permanent rather than something the next pass
silently overwrites.
"""

from __future__ import annotations

import hashlib
import json
import re

# Curly quotes are what the scrapers actually store; straight ones appear in
# some EPUBs. Apostrophes (U+2019) are deliberately excluded — they are far
# more often possessives than quotes.
OPEN_QUOTES = "“‟«\""
CLOSE_QUOTES = "”‟»\""
_QUOTED = re.compile(f"[{re.escape(OPEN_QUOTES)}][^{re.escape(CLOSE_QUOTES)}]{{2,}}"
                     f"[{re.escape(CLOSE_QUOTES)}]")
# An ellipsis ends a chunk too, so the pause after it can be lengthened
# independently. It stays attached to the text before it, which is what marks
# the chunk as wanting the longer beat.
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")

SPEECH_VERBS = (
    "said|asked|replied|answered|muttered|whispered|shouted|yelled|called|"
    "continued|added|snapped|growled|murmured|breathed|hissed|remarked|"
    "observed|admitted|agreed|countered|offered|repeated|stated|declared"
)
# "Gareth said" / "said Gareth" — a capitalised token adjacent to a speech verb.
_TAG_BEFORE_VERB = re.compile(rf"\b([A-Z][\w'-]+)\s+(?:{SPEECH_VERBS})\b")
_TAG_AFTER_VERB = re.compile(rf"\b(?:{SPEECH_VERBS})\s+([A-Z][\w'-]+)\b")

# Words that look like names next to a speech verb but aren't.
_NOT_NAMES = {
    "He", "She", "They", "It", "I", "We", "You", "The", "A", "An", "That",
    "This", "Then", "But", "And", "Who", "What", "There", "His", "Her",
}

NARRATION = "narration"
SPEECH = "speech"


def text_hash(text: str) -> str:
    """Identity of the chapter text a script was built from."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


# "..." and ". . ." are one piece of punctuation, not three sentence endings.
# Splitting inside them produced chunks that were a lone full stop, which a
# TTS model has nothing to say for — Chatterbox answered with hallucinated
# noise. Collapse them to a single ellipsis the splitter won't break apart.
_ELLIPSIS = re.compile(r"\.\s*\.\s*\.[\s.]*|…")
# "H." or "J." is an initial, not the end of a sentence.
_INITIAL = re.compile(r"\b[A-Z]\.$")

ELLIPSIS = "…"
# An ellipsis is a longer beat than a full stop — that is the point of it.
# Three dots, three times the pause. Applied to the gap that follows the
# chunk, so it scales with whatever the voice's sentence pause is set to.
ELLIPSIS_GAP_MULTIPLIER = 3.0


def gap_multiplier(chunk: str) -> float:
    """How much longer than a normal sentence pause this chunk's gap should be."""
    return ELLIPSIS_GAP_MULTIPLIER if chunk.rstrip().endswith(ELLIPSIS) else 1.0


def has_speakable_content(text: str) -> bool:
    """True if there is anything here a voice could actually say."""
    return bool(re.search(r"[A-Za-z0-9]", text))


def normalize_for_speech(text: str) -> str:
    return _ELLIPSIS.sub(f"{ELLIPSIS} ", text)


def split_sentences(text: str) -> list[str]:
    """Sentence split matching how synthesis chunks text, so span index N is
    synthesis chunk N and a voice can be chosen per chunk."""
    out: list[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n|\n", normalize_for_speech(text))
                 if p.strip()):
        for sentence in _SENTENCE.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            # Anything with no letters or digits — stray punctuation left by an
            # unusual construction — belongs to the sentence before it rather
            # than being spoken on its own.
            if out and (not has_speakable_content(sentence) or _INITIAL.search(out[-1])):
                out[-1] = f"{out[-1]} {sentence}".strip()
            elif has_speakable_content(sentence):
                out.append(sentence)
            elif out:
                out[-1] = f"{out[-1]} {sentence}".strip()
    return out


def has_speech(sentence: str) -> bool:
    return bool(_QUOTED.search(sentence))


def guess_speaker_name(sentence: str) -> str | None:
    """Name from an explicit speech tag, or None.

    Deliberately conservative. A wrong voice mid-conversation is worse than a
    neutral one, so anything uncertain is left unattributed for the LLM pass
    or for you to fix.
    """
    for pattern in (_TAG_BEFORE_VERB, _TAG_AFTER_VERB):
        match = pattern.search(sentence)
        if match:
            name = match.group(1)
            if name not in _NOT_NAMES:
                return name
    return None


def build_rule_script(text: str) -> list[dict]:
    """Local, LLM-free script: narration vs speech, plus explicit tags.

    Quote detection is reliable; attribution mostly is not, so most speech
    spans come back with speaker None and fall back to a generic dialogue
    voice at render time.
    """
    spans = []
    for index, sentence in enumerate(split_sentences(text)):
        speech = has_speech(sentence)
        spans.append({
            "i": index,
            "kind": SPEECH if speech else NARRATION,
            "speaker": guess_speaker_name(sentence) if speech else None,
            "confidence": 0.9 if speech else 1.0,
            "source": "rule",
        })
    return spans


def normalize_spans(raw: list[dict], sentence_count: int) -> list[dict]:
    """Coerce externally-supplied spans into the stored shape.

    An external tagger is trusted for attribution, not for structure: indices
    are clamped to the sentences that actually exist, unknown fields dropped,
    and anything malformed skipped rather than allowed to corrupt a script.
    """
    cleaned: dict[int, dict] = {}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if not (0 <= index < sentence_count):
            continue
        kind = item.get("kind")
        if kind not in (NARRATION, SPEECH):
            kind = SPEECH if item.get("speaker") else NARRATION
        speaker = item.get("speaker")
        speaker = str(speaker) if speaker not in (None, "") else None
        try:
            confidence = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        source = item.get("source")
        if source not in ("rule", "external", "manual"):
            source = "external"
        cleaned[index] = {
            "i": index,
            "kind": kind,
            "speaker": speaker,
            "confidence": max(0.0, min(1.0, confidence)),
            "source": source,
        }
    return [cleaned[i] for i in sorted(cleaned)]


def merge_spans(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Apply new attributions without discarding manual corrections."""
    by_index = {s["i"]: s for s in existing}
    for span in incoming:
        current = by_index.get(span["i"])
        if current is not None and current.get("source") == "manual":
            continue  # a human already ruled on this one
        by_index[span["i"]] = span
    return [by_index[i] for i in sorted(by_index)]


def dumps(spans: list[dict]) -> str:
    return json.dumps(spans, separators=(",", ":"))


def loads(blob: str) -> list[dict]:
    try:
        spans = json.loads(blob)
    except Exception:
        return []
    return spans if isinstance(spans, list) else []


def resolve_voices(spans: list[dict], characters: list, narrator_voice: str,
                   dialogue_voice: str | None = None) -> list[str]:
    """Voice id per span.

    Falls back deliberately: an attributed character with a cast voice wins;
    otherwise any speech uses the generic dialogue voice; otherwise the
    narrator. Unknown or uncast characters therefore sound like ordinary
    dialogue rather than breaking the render.
    """
    by_name = {}
    for character in characters:
        if not character.voice:
            continue
        by_name[character.name.casefold()] = character.voice
        for alias in character_aliases(character):
            by_name[alias.casefold()] = character.voice

    voices = []
    for span in spans:
        voice = None
        if span.get("kind") == SPEECH:
            speaker = (span.get("speaker") or "").casefold()
            voice = by_name.get(speaker) or dialogue_voice
        voices.append(voice or narrator_voice)
    return voices


def character_aliases(character) -> list[str]:
    if not character.aliases:
        return []
    try:
        values = json.loads(character.aliases)
    except Exception:
        return []
    return [str(v) for v in values] if isinstance(values, list) else []


def chunk_voices_for_chapter(db, chapter, engine, narrator_voice: str) -> list[str] | None:
    """Voice per synthesis chunk for a chapter, or None to render single-voice.

    Returns None unless the novel is opted in, has a stored script matching the
    current text, and at least one character is actually cast — otherwise
    there is nothing to gain and single-voice rendering stays untouched.

    The mapping relies on span index N being synthesis chunk N. That holds
    because both sides split on the same sentence boundaries; if an engine ever
    chunks differently the count check below fails and we fall back safely.
    """
    from database import Character, ChapterScript, Novel

    novel = db.query(Novel).filter(Novel.id == chapter.novel_id).first()
    if novel is None or not novel.multi_voice or not chapter.text:
        return None

    row = db.query(ChapterScript).filter(
        ChapterScript.chapter_id == chapter.id).first()
    if row is None or row.text_hash != text_hash(chapter.text):
        return None

    characters = db.query(Character).filter(Character.novel_id == novel.id).all()
    if not any(c.voice for c in characters):
        return None

    spans = loads(row.spans)
    chunks = engine.plan_chunks(f"{chapter.title}\n\n{chapter.text}")
    voices = resolve_voices(spans, characters, narrator_voice)

    # The rendered text is prefixed with the chapter title announcement, which
    # is narration and has no span. Offset by however many chunks it took.
    title_chunks = len(engine.plan_chunks(chapter.title))
    aligned = [narrator_voice] * title_chunks + voices
    if len(aligned) != len(chunks):
        return None  # split mismatch — safer to render single-voice
    return aligned


def script_stats(spans: list[dict]) -> dict:
    """Coverage summary — how much is dialogue and how much is attributed."""
    speech = [s for s in spans if s.get("kind") == SPEECH]
    attributed = [s for s in speech if s.get("speaker")]
    manual = [s for s in spans if s.get("source") == "manual"]
    return {
        "sentences": len(spans),
        "speech": len(speech),
        "attributed": len(attributed),
        "manual_overrides": len(manual),
        "attribution_rate": round(len(attributed) / len(speech), 3) if speech else 0.0,
    }
