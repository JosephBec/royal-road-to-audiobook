"""Split chapter text into synthesis batches for the export worker.

Batches are the yield granularity of exports: between batches the worker
re-checks whether playback/prefetch/favorites need the TTS worker.
"""


def split_batches(text: str, max_words: int = 600) -> list[str]:
    """Group non-empty lines into batches of at most max_words words.

    A single line longer than the budget is emitted alone rather than split,
    so Kokoro still sees intact paragraphs.
    """
    lines = [l for l in text.split("\n") if l.strip()]
    batches: list[str] = []
    current: list[str] = []
    count = 0
    for line in lines:
        words = len(line.split())
        if current and count + words > max_words:
            batches.append("\n".join(current))
            current, count = [], 0
        current.append(line)
        count += words
    if current:
        batches.append("\n".join(current))
    return batches
