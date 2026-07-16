"""Document parsing — the one home for bytes that aren't HTML (PDF, EPUB, images).

The scrape pipeline hands raw bytes here when the fetch is a document rather
than a web page.
"""
from typing import Optional
from urllib.parse import urlparse

from app.documents.types import ParsedDoc

__all__ = ["sniff", "parse", "ParsedDoc"]

# URL-path suffixes that identify a standalone image (suffix fallback only —
# an explicit image/* content-type is the primary signal).
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif")


def sniff(content_type: str, url: str) -> Optional[str]:
    """Identify a document by content-type, falling back to URL suffix.

    Content-type wins on any conflict. The suffix is parsed from the URL
    *path* only (urlparse) so a query string cannot spoof a match and a
    fragment cannot hide one.
    """
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return "pdf"
    if "epub" in ct:
        return "epub"
    if ct.startswith("image/"):
        return "image"
    path = urlparse(url or "").path.lower()
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(".epub"):
        return "epub"
    if path.endswith(_IMAGE_SUFFIXES):
        return "image"
    return None


from app.documents import epub, image, pdf  # noqa: E402 — deferred below sniff/parse: pdf.py imports back into this package (app.documents.ocr), so a top-level import would re-enter __init__ before these names are bound

# kind -> parser. Each parser returns a complete ParsedDoc (incl. its own
# `extractor` label) or None on a genuine extraction failure.
_PARSERS = {"pdf": pdf.extract, "epub": epub.extract, "image": image.extract}


def parse(kind: str, data: bytes) -> Optional[ParsedDoc]:
    """Parse document bytes for a sniffed `kind`.

    Returns None only when the document was identified as `kind` but could not
    be extracted (corrupt/encrypted/empty). `kind` values with no parser also
    return None.
    """
    fn = _PARSERS.get(kind)
    return fn(data) if fn else None
