"""scrapers.base description sanitizers: preserve real formatting from
source sites while stripping anything unsafe. See sanitize_description_html's
docstring for why this exists — get_text(strip=True) was throwing away every
paragraph break and hyperlink a novel's description actually had."""
from bs4 import BeautifulSoup

from scrapers.base import sanitize_description_html, text_to_description_html


def _sanitize(html: str) -> str:
    soup = BeautifulSoup(f'<div class="d">{html}</div>', "lxml")
    return sanitize_description_html(soup.select_one(".d"))


def test_keeps_paragraphs_and_formatting():
    # <p> is block-level, so rendering doesn't depend on whitespace between
    # the source tags — assert on the parsed structure, not exact bytes.
    out = _sanitize("<p>First <b>bold</b> paragraph.</p><p>Second <em>paragraph</em>.</p>")
    soup = BeautifulSoup(out, "lxml")
    paras = soup.find_all("p")
    assert len(paras) == 2
    assert paras[0].decode_contents() == "First <b>bold</b> paragraph."
    assert paras[1].decode_contents() == "Second <em>paragraph</em>."


def test_keeps_safe_links_and_adds_target_blank():
    out = _sanitize('<p>Check <a href="https://example.com/promo">this out</a>.</p>')
    assert 'href="https://example.com/promo"' in out
    assert 'target="_blank"' in out
    assert 'rel="noopener noreferrer"' in out


def test_strips_script_and_event_handlers():
    out = _sanitize('<p>Text<script>alert(1)</script> more<img src=x onerror="evil()"></p>')
    assert "script" not in out
    assert "onerror" not in out
    assert "evil" not in out


def test_drops_javascript_and_relative_hrefs():
    out = _sanitize('<p><a href="javascript:alert(1)">bad</a> <a href="/local">also gone</a></p>')
    assert "<a" not in out
    assert "bad" in out and "also gone" in out


def test_unwraps_unknown_tags_keeping_text():
    out = _sanitize('<div>Wrapper <span class="x">text</span></div>')
    assert out == "Wrapper text"


def test_drops_empty_paragraphs():
    out = _sanitize("<p>Real content.</p><p></p><p>   </p>")
    assert out == "<p>Real content.</p>"


def test_empty_input_gives_empty_string():
    assert _sanitize("") == ""


def test_text_to_description_html_wraps_paragraphs():
    out = text_to_description_html("Line one.\nLine two.\n\nSecond paragraph.")
    assert out == "<p>Line one.<br>Line two.</p><p>Second paragraph.</p>"


def test_text_to_description_html_escapes():
    out = text_to_description_html("Safe <b>not bold</b> & fine")
    assert "<b>" not in out
    assert "&amp;" in out
    assert "&lt;b&gt;" in out
