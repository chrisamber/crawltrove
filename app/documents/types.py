"""The one schema every document parser returns. Adding a format reuses this
type so the shape cannot drift. `ocr` is None when OCR was not applicable
(EPUB, text-layer PDF); a dict only when Tesseract actually ran."""
from typing import Optional, TypedDict

try:
    from typing import NotRequired
except ImportError:  # Python 3.10
    from typing_extensions import NotRequired


class ParsedDoc(TypedDict):
    markdown: str
    title: str
    pages: int
    ocr: Optional[dict]
    extractor: str  # "pdf" | "pdf+ocr" | "pdf+ocr+vision" | "epub" | "image+ocr" | "image+ocr+vision"
    # Per-page markdown parts (PDF path only): index == page number, empty
    # pages kept, `markdown` is the join of the non-empty ones. Lets the
    # vision escalation tier replace one page's text and rejoin.
    parts: NotRequired[list]
