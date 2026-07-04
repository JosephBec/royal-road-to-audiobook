from m4b import sanitize_title, export_basename, ffmetadata_content


def test_sanitize_strips_windows_illegal_chars():
    assert sanitize_title('He said: "Go/No*Go?" <now>') == "He said Go No Go now"


def test_sanitize_trims_trailing_dots_and_spaces():
    assert sanitize_title("Book Vol. 2. ") == "Book Vol. 2"


def test_sanitize_empty_falls_back():
    assert sanitize_title("???") == "Untitled"


def test_export_basename():
    assert export_basename("The Hundred Reigns", 33, 74) == "The Hundred Reigns - Chapters 33 - 74"


def test_ffmetadata_chapters_and_escaping():
    content = ffmetadata_content([("Ch 1; a=b", 2.0), ("Ch 2", 1.5)])
    assert content.startswith(";FFMETADATA1")
    assert "START=0" in content and "END=2000" in content
    assert "START=2000" in content and "END=3500" in content
    assert r"title=Ch 1\; a\=b" in content
