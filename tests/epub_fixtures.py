"""Build small real EPUB files for tests (ebooklib writes EPUBs too)."""
from ebooklib import epub

# 30 words — comfortably above epub_local.MIN_CHAPTER_WORDS (20)
LONG_PARA = ("Down by the river the old miller counted his sacks of grain "
             "while the ferryman waited, whistling a tune that nobody on "
             "either bank of the river had ever heard before today.")

# Cover detection requires content > 1000 bytes; a fake JPEG header is enough
COVER_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 2000


def make_epub(path, title="Test Book", author="Test Author",
              chapters=None, cover=None, description=None):
    """Write a minimal valid EPUB. `chapters` is a list of (title, [paragraphs])."""
    if chapters is None:
        chapters = [("Chapter One", [LONG_PARA]), ("Chapter Two", [LONG_PARA])]
    book = epub.EpubBook()
    book.set_identifier(f"test-{title}")
    book.set_title(title)
    book.set_language("en")
    book.add_author(author)
    if description:
        book.add_metadata("DC", "description", description)
    if cover is not None:
        book.set_cover("cover.jpg", cover)
    items = []
    for i, (ch_title, paragraphs) in enumerate(chapters, start=1):
        item = epub.EpubHtml(title=ch_title, file_name=f"ch{i}.xhtml", lang="en")
        body = "".join(f"<p>{p}</p>" for p in paragraphs)
        item.content = f"<html><body><h1>{ch_title}</h1>{body}</body></html>"
        book.add_item(item)
        items.append(item)
    book.toc = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + items
    epub.write_epub(str(path), book)
    return path
