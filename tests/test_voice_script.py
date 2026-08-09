"""Voice scripts: span building, external ingest, and manual precedence.

The load-bearing rule is that a manual correction outlives re-tagging.
Attribution will be wrong sometimes at any model size, so a fix you make by
hand must never be silently overwritten by the next pass.
"""
import json

import pytest

import voice_script as vs

OPEN, CLOSE = "“", "”"


def q(text):
    return f"{OPEN}{text}{CLOSE}"


# ----- sentence splitting must match how synthesis chunks -----

def test_split_matches_chatterbox_chunking():
    """Span index N has to be synthesis chunk N or voices land on wrong lines."""
    from engines.chatterbox_engine import _split_chunks
    text = "First one. Second one.\n\nThird one here. Fourth one."
    assert vs.split_sentences(text) == _split_chunks(text)


def test_blank_lines_do_not_produce_spans():
    assert vs.split_sentences("One.\n\n\n  \n\nTwo.") == ["One.", "Two."]


# ----- narration vs speech -----

def test_quoted_text_is_speech():
    spans = vs.build_rule_script(f"He waited. {q('You are late.')}")
    assert [s["kind"] for s in spans] == [vs.NARRATION, vs.SPEECH]


def test_apostrophes_are_not_treated_as_quotes():
    """U+2019 is overwhelmingly a possessive, not dialogue."""
    spans = vs.build_rule_script("The captain’s ledger lay open on the desk.")
    assert spans[0]["kind"] == vs.NARRATION


def test_explicit_tag_is_attributed():
    spans = vs.build_rule_script(f"{q('We should go.')} said Gareth.")
    assert spans[0]["speaker"] == "Gareth"


def test_pronouns_are_not_mistaken_for_names():
    """'He said' must not become a character called He."""
    spans = vs.build_rule_script(f"{q('Not yet.')} he said, shaking his head.")
    assert spans[0]["speaker"] is None


def test_unattributed_dialogue_stays_unattributed():
    """Guessing wrong mid-conversation is worse than staying neutral."""
    spans = vs.build_rule_script(q("And what if it doesn't work?"))
    assert spans[0]["kind"] == vs.SPEECH
    assert spans[0]["speaker"] is None


# ----- ingest hardening -----

def test_out_of_range_spans_are_dropped():
    assert vs.normalize_spans([{"i": 99, "speaker": "X"}], 3) == []


def test_malformed_spans_are_skipped_not_fatal():
    raw = [{"i": "nope"}, None, {"i": 0, "speaker": "Ana"}, "junk"]
    out = vs.normalize_spans(raw, 2)
    assert [s["i"] for s in out] == [0]
    assert out[0]["speaker"] == "Ana"


def test_confidence_is_clamped():
    out = vs.normalize_spans([{"i": 0, "speaker": "A", "confidence": 7}], 1)
    assert out[0]["confidence"] == 1.0


def test_speaker_implies_speech():
    out = vs.normalize_spans([{"i": 0, "speaker": "Ana"}], 1)
    assert out[0]["kind"] == vs.SPEECH


# ----- manual precedence: the whole point of the source field -----

def test_manual_span_survives_retagging():
    existing = [{"i": 0, "kind": vs.SPEECH, "speaker": "Ana",
                 "confidence": 1.0, "source": "manual"}]
    incoming = [{"i": 0, "kind": vs.SPEECH, "speaker": "Bob",
                 "confidence": 0.8, "source": "external"}]
    merged = vs.merge_spans(existing, incoming)
    assert merged[0]["speaker"] == "Ana"


def test_automatic_spans_are_replaced_by_retagging():
    existing = [{"i": 0, "kind": vs.SPEECH, "speaker": None,
                 "confidence": 0.5, "source": "rule"}]
    incoming = [{"i": 0, "kind": vs.SPEECH, "speaker": "Bob",
                 "confidence": 0.9, "source": "external"}]
    assert vs.merge_spans(existing, incoming)[0]["speaker"] == "Bob"


def test_merge_keeps_spans_ordered():
    existing = [{"i": i, "kind": vs.NARRATION, "speaker": None,
                 "confidence": 1.0, "source": "rule"} for i in range(4)]
    incoming = [{"i": 2, "kind": vs.SPEECH, "speaker": "Z",
                 "confidence": 1.0, "source": "external"}]
    assert [s["i"] for s in vs.merge_spans(existing, incoming)] == [0, 1, 2, 3]


# ----- casting -----

class FakeCharacter:
    def __init__(self, name, voice, aliases=None):
        self.name, self.voice = name, voice
        self.aliases = json.dumps(aliases) if aliases else None


def test_attributed_speech_uses_the_character_voice():
    spans = [{"i": 0, "kind": vs.SPEECH, "speaker": "Ana"}]
    assert vs.resolve_voices(spans, [FakeCharacter("Ana", "cb_yearsley")],
                             "cb_builtin") == ["cb_yearsley"]


def test_aliases_resolve_to_the_same_voice():
    spans = [{"i": 0, "kind": vs.SPEECH, "speaker": "the captain"}]
    chars = [FakeCharacter("Ana", "cb_geeson", aliases=["the captain"])]
    assert vs.resolve_voices(spans, chars, "cb_builtin") == ["cb_geeson"]


def test_narration_always_uses_the_narrator():
    spans = [{"i": 0, "kind": vs.NARRATION, "speaker": None}]
    assert vs.resolve_voices(spans, [FakeCharacter("Ana", "x")],
                             "narrator") == ["narrator"]


def test_uncast_character_falls_back_rather_than_failing():
    spans = [{"i": 0, "kind": vs.SPEECH, "speaker": "Nobody"}]
    assert vs.resolve_voices(spans, [], "narrator") == ["narrator"]
    assert vs.resolve_voices(spans, [], "narrator", "dialogue") == ["dialogue"]


# ----- text hash pins a script to its chapter -----

def test_hash_changes_when_text_changes():
    assert vs.text_hash("One. Two.") != vs.text_hash("One. Three.")


def test_stats_report_attribution_coverage():
    spans = vs.build_rule_script(
        f"She waited. {q('Where were you?')} asked Mira. {q('Out.')}")
    stats = vs.script_stats(spans)
    assert stats["sentences"] == 3
    assert stats["speech"] == 2
    assert stats["attributed"] == 1
    assert stats["attribution_rate"] == 0.5
