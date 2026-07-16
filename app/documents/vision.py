"""Vision-LLM OCR escalation tier — off by default (spec §12).

Tesseract's plain-text output has an accuracy/structure ceiling on hard scans.
When enabled, this tier re-renders only the pages Tesseract was unsure about
(confidence below OCR_VISION_MIN_CONF, or unreadable) and asks a vision LLM to
transcribe them, via the same dual-backend waterfall as /api/extract
(app/extract_llm.py: local OpenAI-compatible first, then Anthropic).

Gates (ALL must hold, else the tesseract result passes through untouched):
- env OCR_VISION_ENABLED (default off — parsed like OCR_ENABLED, flipped default)
- an LLM backend is configured (extract_llm.configured())
- the document actually has OCR'd pages (text-layer PDFs never escalate)

Resilient-signal invariant: the WHOLE pass is wrapped — any failure (per page
or per document) logs and returns the tesseract version; escalation can only
ever improve a page, never fail a scrape.

Env knobs (read per call so they're testable/tunable without a restart):
- OCR_VISION_ENABLED   default off — master switch for this tier
- OCR_VISION_MIN_CONF  default 55 — escalate pages below this 0-100 confidence
- OCR_VISION_MAX_PAGES default 5  — per-document cap on vision calls
"""
import base64
import io
import logging
import os
from typing import Optional

import pypdfium2 as pdfium
from PIL import Image

from app import extract_llm
from app.documents import ocr
from app.documents.types import ParsedDoc

logger = logging.getLogger("vision")


def _enabled() -> bool:
    # Same parse as OCR_ENABLED in pdf.py, but defaulting OFF: any value other
    # than "false" enables — the default is "false".
    return os.getenv("OCR_VISION_ENABLED", "false").lower() != "false"


def _min_conf() -> float:
    return float(os.getenv("OCR_VISION_MIN_CONF", "55"))


def _max_pages() -> int:
    return int(os.getenv("OCR_VISION_MAX_PAGES", "5"))


def _png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _media_type(data: bytes) -> str:
    """Best-effort media type of raw image bytes via PIL; PNG fallback."""
    try:
        fmt = Image.open(io.BytesIO(data)).format
        return Image.MIME.get(fmt, "image/png")
    except Exception:
        return "image/png"


async def escalate(parsed: ParsedDoc, data: bytes, kind: str) -> ParsedDoc:
    """Escalate low-confidence OCR'd pages of `parsed` to the vision LLM.

    Returns the (possibly improved) ParsedDoc. Never raises: any failure —
    gate, render, transport, model — returns the tesseract version unchanged.
    """
    try:
        if not _enabled() or not extract_llm.configured():
            return parsed
        if not parsed or not parsed.get("ocr"):
            return parsed  # nothing was OCR'd — nothing to escalate
        if kind == "pdf":
            return await _escalate_pdf(parsed, data)
        if kind == "image":
            return await _escalate_image(parsed, data)
        return parsed
    except Exception as e:  # resilient-signal invariant: degrade, never fail
        logger.warning("vision escalation failed (%s): %s", kind, e)
        return parsed


async def _escalate_pdf(parsed: ParsedDoc, data: bytes) -> ParsedDoc:
    ocr_info = parsed["ocr"]
    min_conf = _min_conf()
    flagged = [e for e in ocr_info.get("pages_ocred", [])
               if e.get("confidence") is None or e["confidence"] < min_conf]
    flagged = flagged[:_max_pages()]
    parts = parsed.get("parts")
    if not flagged or not parts:
        return parsed

    dpi = int(os.getenv("OCR_DPI", "300"))
    improved = False
    doc = pdfium.PdfDocument(data)
    try:
        for entry in flagged:
            # Per-page guard: a vision failure keeps that page's tesseract
            # text and the loop continues.
            try:
                i = entry["page"]
                page = doc[i]
                try:
                    img = ocr._render(page, dpi)
                finally:
                    page.close()
                text = await extract_llm.transcribe_image(_png_b64(img), "image/png")
                if text and text.strip():
                    parts[i] = text.strip()
                    entry["engine"] = "vision"
                    improved = True
            except Exception as e:
                logger.warning("vision transcription failed for page %s: %s",
                               entry.get("page"), e)
    finally:
        doc.close()

    if improved:
        ocr_info["engine"] = "tesseract+vision"
        parsed["markdown"] = "\n\n".join(p for p in parts if p).strip()
        parsed["extractor"] = "pdf+ocr+vision"
    return parsed


async def _escalate_image(parsed: ParsedDoc, data: bytes) -> ParsedDoc:
    # A standalone image is its own single "page" — send the original bytes.
    ocr_info = parsed["ocr"]
    entries = ocr_info.get("pages_ocred") or []
    min_conf = _min_conf()
    if entries and not any(e.get("confidence") is None or e["confidence"] < min_conf
                           for e in entries):
        return parsed  # tesseract was confident — no escalation needed
    b64 = base64.b64encode(data).decode("ascii")
    text = await extract_llm.transcribe_image(b64, _media_type(data))
    if text and text.strip():
        parsed["markdown"] = text.strip()
        for entry in entries:
            entry["engine"] = "vision"
        ocr_info["engine"] = "tesseract+vision"
        parsed["extractor"] = "image+ocr+vision"
    return parsed
