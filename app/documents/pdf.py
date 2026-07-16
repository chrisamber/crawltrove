"""PDF text extraction with per-page Tesseract fallback."""

import logging
import os
from typing import Optional

import pypdfium2 as pdfium

from app.documents import ocr
from app.documents.types import ParsedDoc

logger = logging.getLogger("pdf")

MAX_PAGES = 100

OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() != "false"
OCR_OSD = os.getenv("OCR_OSD", "true").lower() != "false"
OCR_LANGUAGES = os.getenv("OCR_LANGUAGES", "eng")
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "25"))
OCR_MIN_CHARS = int(os.getenv("OCR_MIN_CHARS", "20"))


def _text(page) -> str:
    textpage = page.get_textpage()
    try:
        return (textpage.get_text_bounded() or "").replace("\r\n", "\n")
    finally:
        textpage.close()


def extract(data: bytes) -> Optional[ParsedDoc]:
    """Return per-page text and OCR metadata, or None for invalid/empty PDFs."""
    try:
        doc = pdfium.PdfDocument(data)
        try:
            pages = min(len(doc), MAX_PAGES)
            metadata = doc.get_metadata_dict(skip_empty=True)
            title = metadata.get("Title") or ""
            ocred, parts, thin_seen, attempts = [], [], 0, 0

            for i in range(pages):
                page_md = ""
                try:
                    page = doc[i]
                except Exception:
                    parts.append(page_md)
                    continue
                try:
                    try:
                        text_layer = _text(page).strip()
                    except Exception:
                        text_layer = ""
                    page_md = text_layer
                    if OCR_ENABLED and len(text_layer) < OCR_MIN_CHARS:
                        thin_seen += 1
                        if attempts < OCR_MAX_PAGES:
                            attempts += 1
                            result = ocr.ocr_page(
                                page,
                                default_languages=OCR_LANGUAGES,
                                dpi=OCR_DPI,
                                use_osd=OCR_OSD,
                            )
                            if result and result["text"].strip():
                                page_md = result["text"].strip()
                                ocred.append({
                                    "page": i,
                                    "engine": "tesseract",
                                    "script": result["script"],
                                    "languages": result["languages"],
                                    "confidence": result["confidence"],
                                })
                except Exception:
                    pass
                finally:
                    page.close()
                parts.append(page_md)
        finally:
            doc.close()
    except Exception:
        return None

    truncated = thin_seen > OCR_MAX_PAGES
    if truncated:
        logger.warning(
            "OCR truncated: %d image-only pages but OCR_MAX_PAGES=%d; %d page(s) left un-OCR'd",
            thin_seen,
            OCR_MAX_PAGES,
            thin_seen - OCR_MAX_PAGES,
        )

    markdown = "\n\n".join(part for part in parts if part).strip()
    if not markdown:
        return None

    ocr_info = None
    if ocred:
        confidences = [entry["confidence"] for entry in ocred
                       if entry["confidence"] is not None]
        ocr_info = {
            "engine": "tesseract",
            "pages_ocred": ocred,
            "pages_ocred_count": len(ocred),
            "languages": list(dict.fromkeys(entry["languages"] for entry in ocred)),
            "mean_confidence": (
                round(sum(confidences) / len(confidences), 1) if confidences else None
            ),
            "truncated": truncated,
        }

    return {
        "markdown": markdown,
        "title": title.strip(),
        "pages": pages,
        "ocr": ocr_info,
        "parts": parts,
        "extractor": "pdf+ocr" if ocr_info else "pdf",
    }
