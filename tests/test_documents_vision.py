"""Vision-LLM OCR escalation tier (app/documents/vision.py).

All hermetic: extract_llm.transcribe_image and .configured are monkeypatched
(no backend, no network), tesseract is never invoked (parsed docs are built by
hand), and PDFium renders dependency-free fixture PDFs locally.
"""
import copy
import io

from PIL import Image as PILImage

from app import extract_llm
from app.documents import ocr, pdf, vision
from tests.pdf_fixture import make_pdf


# --- fixtures ---------------------------------------------------------------

def _make_pdf(pages=2) -> bytes:
    return make_pdf(*(f"page {i}" for i in range(pages)))


def _make_png() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (40, 20), "white").save(buf, format="PNG")
    return buf.getvalue()


def _parsed_pdf(parts, ocred):
    return {
        "markdown": "\n\n".join(p for p in parts if p).strip(),
        "title": "", "pages": len(parts), "parts": list(parts),
        "ocr": {"engine": "tesseract", "pages_ocred": ocred,
                "pages_ocred_count": len(ocred), "languages": ["eng"],
                "mean_confidence": 50.0, "truncated": False},
        "extractor": "pdf+ocr",
    }


def _parsed_image(text="tesseract read", confidence=30.0):
    return {
        "markdown": text, "title": "", "pages": 1,
        "ocr": {"engine": "tesseract",
                "pages_ocred": [{"page": 0, "engine": "tesseract",
                                 "script": "Latin", "languages": "eng",
                                 "confidence": confidence}],
                "pages_ocred_count": 1, "languages": ["eng"],
                "mean_confidence": confidence, "truncated": False},
        "extractor": "image+ocr",
    }


def _wire(monkeypatch, *, enabled=True, configured=True, reply="VISION TEXT",
          raise_=False):
    """Enable the tier and stub the LLM; returns the call-capture list."""
    calls = []

    async def fake_transcribe(image_b64, media_type, **kw):
        calls.append({"b64": image_b64, "media_type": media_type})
        if raise_:
            raise RuntimeError("backend down")
        return reply

    if enabled:
        monkeypatch.setenv("OCR_VISION_ENABLED", "true")
    else:
        monkeypatch.delenv("OCR_VISION_ENABLED", raising=False)
    monkeypatch.setattr(extract_llm, "configured", lambda: configured)
    monkeypatch.setattr(extract_llm, "transcribe_image", fake_transcribe)
    return calls


# --- (a) low-confidence page replaced ----------------------------------------

async def test_low_confidence_page_escalates(monkeypatch):
    calls = _wire(monkeypatch)
    parsed = _parsed_pdf(
        ["junk ocr", "good page text"],
        [{"page": 0, "engine": "tesseract", "script": "Latin",
          "languages": "eng", "confidence": 30.0}])
    res = await vision.escalate(parsed, _make_pdf(), "pdf")
    assert len(calls) == 1
    assert calls[0]["media_type"] == "image/png"
    assert res["parts"][0] == "VISION TEXT"          # flagged page replaced...
    assert res["parts"][1] == "good page text"       # ...others untouched
    assert res["markdown"] == "VISION TEXT\n\ngood page text"  # rejoined
    assert res["ocr"]["pages_ocred"][0]["engine"] == "vision"
    assert res["ocr"]["engine"] == "tesseract+vision"
    assert res["extractor"] == "pdf+ocr+vision"


async def test_none_confidence_counts_as_unsure(monkeypatch):
    calls = _wire(monkeypatch)
    parsed = _parsed_pdf(
        ["unsure text"],
        [{"page": 0, "engine": "tesseract", "script": None,
          "languages": "eng", "confidence": None}])
    res = await vision.escalate(parsed, _make_pdf(1), "pdf")
    assert len(calls) == 1
    assert res["markdown"] == "VISION TEXT"


# --- (b) high-confidence pages untouched --------------------------------------

async def test_high_confidence_pages_untouched(monkeypatch):
    calls = _wire(monkeypatch)
    parsed = _parsed_pdf(
        ["confident ocr text", "good page text"],
        [{"page": 0, "engine": "tesseract", "script": "Latin",
          "languages": "eng", "confidence": 92.0}])
    before = copy.deepcopy(parsed)
    res = await vision.escalate(parsed, _make_pdf(), "pdf")
    assert calls == []                               # LLM never called
    assert res == before                             # nothing changed


# --- (c) off by default --------------------------------------------------------

async def test_disabled_by_default_never_calls_llm(monkeypatch):
    calls = _wire(monkeypatch, enabled=False)        # OCR_VISION_ENABLED unset
    parsed = _parsed_pdf(
        ["junk ocr"],
        [{"page": 0, "engine": "tesseract", "script": "Latin",
          "languages": "eng", "confidence": 10.0}])
    before = copy.deepcopy(parsed)
    res = await vision.escalate(parsed, _make_pdf(1), "pdf")
    assert calls == []
    assert res == before


async def test_unconfigured_backend_never_calls_llm(monkeypatch):
    calls = _wire(monkeypatch, configured=False)
    parsed = _parsed_pdf(
        ["junk ocr"],
        [{"page": 0, "engine": "tesseract", "script": "Latin",
          "languages": "eng", "confidence": 10.0}])
    before = copy.deepcopy(parsed)
    res = await vision.escalate(parsed, _make_pdf(1), "pdf")
    assert calls == []
    assert res == before


# --- (d) transcribe raising → tesseract text kept -------------------------------

async def test_transcribe_failure_keeps_tesseract_text(monkeypatch):
    _wire(monkeypatch, raise_=True)
    parsed = _parsed_pdf(
        ["junk ocr", "good page text"],
        [{"page": 0, "engine": "tesseract", "script": "Latin",
          "languages": "eng", "confidence": 30.0}])
    before = copy.deepcopy(parsed)
    res = await vision.escalate(parsed, _make_pdf(), "pdf")  # must not raise
    assert res["markdown"] == before["markdown"]     # tesseract text kept
    assert res["ocr"]["engine"] == "tesseract"       # no vision label
    assert res["extractor"] == "pdf+ocr"


async def test_corrupt_pdf_bytes_degrade_to_tesseract(monkeypatch):
    _wire(monkeypatch)
    parsed = _parsed_pdf(
        ["junk ocr"],
        [{"page": 0, "engine": "tesseract", "script": "Latin",
          "languages": "eng", "confidence": 30.0}])
    before = copy.deepcopy(parsed)
    res = await vision.escalate(parsed, b"not a pdf", "pdf")  # whole-pass wrap
    assert res == before


# --- (e) image kind --------------------------------------------------------------

async def test_image_kind_escalates_whole_image(monkeypatch):
    calls = _wire(monkeypatch, reply="IMAGE VISION TEXT")
    data = _make_png()
    res = await vision.escalate(_parsed_image(confidence=20.0), data, "image")
    assert len(calls) == 1
    assert calls[0]["media_type"] == "image/png"     # PIL-sniffed media type
    assert res["markdown"] == "IMAGE VISION TEXT"    # whole markdown replaced
    assert res["ocr"]["engine"] == "tesseract+vision"
    assert res["ocr"]["pages_ocred"][0]["engine"] == "vision"
    assert res["extractor"] == "image+ocr+vision"


async def test_image_kind_confident_tesseract_skips_llm(monkeypatch):
    calls = _wire(monkeypatch)
    parsed = _parsed_image(confidence=95.0)
    before = copy.deepcopy(parsed)
    res = await vision.escalate(parsed, _make_png(), "image")
    assert calls == []
    assert res == before


# --- pdf.py seam regression (parts + per-entry engine) ------------------------
# These live here (not in test_documents_pdf.py) because they exercise the
# contract shared by PDF extraction and vision escalation.

def test_pdf_parts_join_is_markdown():
    """`parts` is additive: the returned markdown must stay exactly the join of
    the non-empty parts (regression guard for the vision-escalation seam)."""
    res = pdf.extract(make_pdf(*(f"page number {i} content" for i in range(3))))
    assert res is not None
    assert len(res["parts"]) == 3  # index == page number
    assert res["markdown"] == "\n\n".join(p for p in res["parts"] if p).strip()


def test_pdf_pages_ocred_entries_carry_engine(monkeypatch):
    """Each pages_ocred entry names its engine (tesseract) so the vision tier
    can relabel escalated pages per page. Tesseract is not installed here —
    fake the page-level OCR call; the blank page trips the thin-text gate."""
    monkeypatch.setattr(ocr, "ocr_page", lambda page, **kw: {
        "text": "recovered scan text", "confidence": 40.0,
        "languages": "eng", "script": "Latin"})
    res = pdf.extract(make_pdf())  # blank page — empty text layer → OCR gate fires
    assert res is not None
    assert res["extractor"] == "pdf+ocr"
    assert res["ocr"]["pages_ocred"] == [
        {"page": 0, "engine": "tesseract", "script": "Latin",
         "languages": "eng", "confidence": 40.0}]
    assert res["parts"] == ["recovered scan text"]
    assert res["markdown"] == "recovered scan text"


# --- cap ---------------------------------------------------------------------

async def test_vision_max_pages_caps_llm_calls(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setenv("OCR_VISION_MAX_PAGES", "1")
    parsed = _parsed_pdf(
        ["junk a", "junk b"],
        [{"page": 0, "engine": "tesseract", "script": "Latin",
          "languages": "eng", "confidence": 10.0},
         {"page": 1, "engine": "tesseract", "script": "Latin",
          "languages": "eng", "confidence": 10.0}])
    res = await vision.escalate(parsed, _make_pdf(), "pdf")
    assert len(calls) == 1                           # capped
    assert res["parts"] == ["VISION TEXT", "junk b"]
    assert res["ocr"]["pages_ocred"][1]["engine"] == "tesseract"
