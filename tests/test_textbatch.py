from textbatch import split_batches


def test_short_text_is_one_batch():
    assert split_batches("hello world\nsecond line") == ["hello world\nsecond line"]


def test_splits_on_line_boundaries_at_budget():
    lines = [f"word {' x' * 9}" for _ in range(100)]  # 10 words per line
    batches = split_batches("\n".join(lines), max_words=25)
    assert all(len(b.split()) <= 25 for b in batches)
    assert sum(len(b.split("\n")) for b in batches) == 100


def test_single_huge_line_stays_intact():
    huge = "w " * 1000
    batches = split_batches(huge.strip(), max_words=600)
    assert len(batches) == 1


def test_no_content_lost():
    text = "a\n\nb\nc"
    joined = "\n".join(split_batches(text, max_words=1))
    assert [l for l in joined.split("\n") if l.strip()] == ["a", "b", "c"]
