"""Standalone image ingestion — OCR an image URL into markdown text.

Without this, an image URL falls through `documents.sniff` (None), trips the
`needs_browser` escalation gate on its non-HTML content-type, and lands in
Playwright — which downloads binaries instead of rendering them, surfacing as
a 502. Treating images as documents keeps them on the HTTP tier: the bytes go
straight to Tesseract (script auto-detect via app/documents/ocr.py), mirroring
the PDF image-page path. Resilient by design: any failure (corrupt bytes, OCR
unavailable, nothing readable) returns None and the caller degrades on the
HTTP tier — it never escalates to the browser.
"""
import io
import os
from typing import Optional

from PIL import Image

from app.documents import ocr
from app.documents.types import ParsedDoc

# Same env knobs as the PDF OCR path (spec §8); DPI doesn't apply — the image
# is already a bitmap.
OCR_ENABLED   = os.getenv("OCR_ENABLED", "true").lower() != "false"
OCR_OSD       = os.getenv("OCR_OSD", "true").lower() != "false"
OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "eng")


def extract(data: bytes) -> Optional[ParsedDoc]:
    """OCR image bytes into a ParsedDoc, or None on any failure.

    The `ocr` block matches the PDF path's shape (pdf.py) so the metadata is
    uniform across document kinds; a standalone image is "page 0" of a
    one-page document.
    """
    try:
        if not OCR_ENABLED:
            return None
        img = Image.open(io.BytesIO(data))
        img.load()  # force a full decode so corrupt/truncated bytes fail here
        r = ocr.ocr_image(img, default_languages=OCR_LANGUAGES, use_osd=OCR_OSD)
        if not r or not r["text"].strip():
            return None
        return {
            "markdown": r["text"].strip(),
            "title": "",
            "pages": 1,
            "ocr": {
                "engine": "tesseract",
                "pages_ocred": [{"page": 0, "engine": "tesseract",
                                 "script": r["script"],
                                 "languages": r["languages"],
                                 "confidence": r["confidence"]}],
                "pages_ocred_count": 1,
                "languages": [r["languages"]],
                "mean_confidence": r["confidence"],
                "truncated": False,
            },
            "extractor": "image+ocr",
        }
    except Exception:
        return None
