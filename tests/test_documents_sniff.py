import pytest

from app import documents


@pytest.mark.parametrize("content_type,url,expected", [
    # content-type wins
    ("application/pdf", "https://x/doc", "pdf"),
    ("application/epub+zip", "https://x/doc", "epub"),
    ("application/pdf; charset=binary", "https://x/a", "pdf"),
    # content-type beats a conflicting suffix
    ("application/pdf", "https://x/book.epub", "pdf"),
    # suffix fallback when servers mislabel the content-type
    ("application/octet-stream", "https://x/book.epub", "epub"),
    ("application/zip", "https://x/book.epub", "epub"),
    ("", "https://x/paper.pdf", "pdf"),
    # query cannot spoof a suffix (path-only parse)
    ("application/octet-stream", "https://x/page?file=y.epub", None),
    # fragment cannot hide a real suffix
    ("", "https://x/a.pdf#section", "pdf"),
    # a legit download query after a real suffix still matches
    ("application/octet-stream", "https://x/book.epub?download=1", "epub"),
    # images by content-type (generic image/ prefix)
    ("image/png", "https://x/pic", "image"),
    ("image/jpeg", "https://x/pic", "image"),
    ("image/webp; charset=binary", "https://x/pic", "image"),
    ("image/tiff", "https://x/pic", "image"),
    ("image/gif", "https://x/pic", "image"),
    # content-type beats a conflicting image suffix
    ("application/pdf", "https://x/pic.png", "pdf"),
    # image suffix fallback when servers mislabel the content-type
    ("application/octet-stream", "https://x/pic.png", "image"),
    ("", "https://x/pic.jpg", "image"),
    ("", "https://x/pic.jpeg", "image"),
    ("", "https://x/pic.webp", "image"),
    ("", "https://x/pic.tif", "image"),
    ("", "https://x/pic.tiff", "image"),
    ("", "https://x/pic.bmp", "image"),
    ("", "https://x/pic.gif", "image"),
    # query cannot spoof an image suffix either
    ("application/octet-stream", "https://x/page?file=y.png", None),
    # nothing matches
    ("text/html", "https://x/page", None),
    ("", "https://x/page", None),
])
def test_sniff(content_type, url, expected):
    assert documents.sniff(content_type, url) == expected
