"""
Per-novel text rules: rewriting notation that reads well but speaks badly.

Progression fiction leans on visual shorthand. A skill name with an arrow
between two numbers means "this went up" to a reader and means nothing at all
to a narrator — the arrow simply isn't spoken. A tier written as a Roman
numeral is read as a letter. These rules rewrite that notation into words
before synthesis, without touching the stored chapter text, so a rule can be
changed or removed at any time and the next render picks it up.

Two kinds:

    regex   a pattern and a replacement, with \\1 backreferences
    roman   the same, but any backreferenced group that is a Roman numeral is
            converted to its number first — "Stealth V" becomes "Stealth tier 5"

Rules are deliberately not applied at scrape time. Chapter text is cached
once and forever; baking rules into it would mean a bad rule permanently
corrupts the source, and fixing one would require re-scraping.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MAX_PATTERN_LENGTH = 500
# Roman numerals up to 3999, anchored so ordinary words never match.
_ROMAN_RE = re.compile(
    r"^M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$", re.IGNORECASE)
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
BOUNDARY = chr(92) + "b"   # the regex word-boundary token
_BACKREF = re.compile(r"\\(\d)")


def is_roman(token: str) -> bool:
    token = (token or "").strip()
    return bool(token) and bool(_ROMAN_RE.match(token))


def roman_to_int(token: str) -> int | None:
    """Convert a Roman numeral to its value, or None if it isn't one."""
    token = (token or "").strip().upper()
    if not is_roman(token):
        return None
    total = 0
    previous = 0
    for char in reversed(token):
        value = _ROMAN_VALUES[char]
        total += -value if value < previous else value
        previous = max(previous, value)
    return total or None


def literal_pattern(word: str) -> str:
    """Whole-word, case-insensitive pattern for a literal respelling.

    Built here rather than asked of the user: a name is not a regular
    expression, and one containing a '.' or '(' would otherwise match things
    it shouldn't.
    """
    word = word.strip()
    # A word boundary only exists next to a word character. "Dr." ends in a
    # period, so a trailing boundary could never match and the rule would
    # silently do nothing. Anchor only the sides that can be anchored.
    lead = BOUNDARY if word[:1].isalnum() or word[:1] == "_" else ""
    tail = BOUNDARY if word[-1:].isalnum() or word[-1:] == "_" else ""
    return "(?i)" + lead + re.escape(word) + tail


def validate(kind: str, pattern: str, replacement: str) -> str | None:
    """Reason the rule is unusable, or None if it's fine."""
    if kind not in ("regex", "roman", "literal"):
        return f"unknown rule kind '{kind}'"
    if not pattern:
        return "pattern is required"
    if kind == "literal":
        if not replacement.strip():
            return "a respelling is required"
        return None
    if len(pattern) > MAX_PATTERN_LENGTH:
        return f"pattern is longer than {MAX_PATTERN_LENGTH} characters"
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"invalid regular expression: {e}"
    # A pattern that matches nothing consumes the whole text on every call.
    if compiled.match(""):
        return "pattern matches the empty string, which would loop endlessly"
    highest = max((int(g) for g in _BACKREF.findall(replacement)), default=0)
    if highest > compiled.groups:
        return (f"replacement refers to \\{highest} but the pattern has "
                f"{compiled.groups} group(s)")
    return None


def _substitute(match: re.Match, replacement: str, convert_roman: bool) -> str:
    def resolve(ref: re.Match) -> str:
        index = int(ref.group(1))
        try:
            value = match.group(index) or ""
        except (IndexError, re.error):
            return ""
        if convert_roman:
            number = roman_to_int(value)
            if number is not None:
                return str(number)
        return value
    return _BACKREF.sub(resolve, replacement)


def apply_rule(text: str, kind: str, pattern: str, replacement: str) -> str:
    """Apply one rule. A rule that fails is skipped, never fatal — a bad rule
    should cost you a mispronounced line, not a chapter that won't render."""
    if kind == "literal":
        pattern = literal_pattern(pattern)
    try:
        compiled = re.compile(pattern)
    except re.error:
        logger.warning("Skipping text rule with invalid pattern: %r", pattern)
        return text
    convert_roman = kind == "roman"
    try:
        return compiled.sub(
            lambda m: _substitute(m, replacement, convert_roman), text)
    except Exception:
        logger.exception("Text rule failed on pattern %r", pattern)
        return text


def apply_rules(text: str, rules) -> str:
    """Apply an ordered sequence of rules. `rules` are objects with kind,
    pattern, replacement and enabled attributes."""
    for rule in rules:
        if not getattr(rule, "enabled", True):
            continue
        text = apply_rule(text, rule.kind, rule.pattern, rule.replacement)
    return text


def rules_for_novel(db, novel_id: int | None):
    """Global rules first, then the novel's own, each in sort order.

    Global first so a novel-specific rule can act on the result — the general
    case is handled once and the exceptions layered on top.
    """
    from database import TextRule

    query = db.query(TextRule).filter(TextRule.enabled.is_(True))
    globals_ = (query.filter(TextRule.novel_id.is_(None))
                .order_by(TextRule.sort_order, TextRule.id).all())
    if novel_id is None:
        return globals_
    specific = (query.filter(TextRule.novel_id == novel_id)
                .order_by(TextRule.sort_order, TextRule.id).all())
    return globals_ + specific


def speech_text(db, novel_id: int | None, text: str) -> str:
    """Chapter text as it should be spoken."""
    if not text:
        return text
    return apply_rules(text, rules_for_novel(db, novel_id))


def preview(text: str, kind: str, pattern: str, replacement: str, limit: int = 8):
    """Sample of what a rule would change, for checking before saving it.

    Returns (match_count, examples) where each example is the matched text and
    what it becomes — enough to see a bad pattern before it reaches your ears.
    """
    problem = validate(kind, pattern, replacement)
    if problem:
        raise ValueError(problem)
    if kind == "literal":
        pattern = literal_pattern(pattern)
    compiled = re.compile(pattern)
    convert_roman = kind == "roman"
    examples = []
    count = 0
    for match in compiled.finditer(text or ""):
        count += 1
        if len(examples) < limit:
            examples.append({
                "before": match.group(0),
                "after": _substitute(match, replacement, convert_roman),
            })
    return count, examples


def speech_text_for_chapter(db, chapter) -> str:
    """Title announcement plus rule-applied body: the exact text to synthesize.

    Every render path goes through here so they cannot disagree. They must
    agree: chunk index N has to mean the same sentence to playback, to the
    resume fingerprint, and to a voice script.
    """
    body = speech_text(db, chapter.novel_id, chapter.text or "")
    return f"{chapter.title}\n\n{body}"
