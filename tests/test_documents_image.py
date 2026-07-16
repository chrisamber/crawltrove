"""Standalone image OCR (app/documents/image.py).

The tesseract binary is NOT installed in the test environment, so every test
monkeypatches `ocr.ocr_image` — nothing here may invoke real Tesseract.
"""
import io

from PIL import Image as PILImage

from app.documents import image, ocr


def _make_png(size=(40, 20)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


def test_image_extract_happy_path(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_image", lambda img, **kw: {
        "text": "hello from the image", "confidence": 91.4,
        "languages": "eng", "script": "Latin"})
    res = image.extract(_make_png())
    assert res is not None
    assert res["markdown"] == "hello from the image"
    assert res["title"] == ""
    assert res["pages"] == 1
    assert res["extractor"] == "image+ocr"
    # ocr block mirrors pdf.py's shape
    assert res["ocr"]["engine"] == "tesseract"
    assert res["ocr"]["pages_ocred"] == [
        {"page": 0, "engine": "tesseract", "script": "Latin",
         "languages": "eng", "confidence": 91.4}]
    assert res["ocr"]["pages_ocred_count"] == 1
    assert res["ocr"]["languages"] == ["eng"]
    assert res["ocr"]["mean_confidence"] == 91.4
    assert res["ocr"]["truncated"] is False


def test_image_extract_corrupt_bytes_returns_none(monkeypatch):
    # PIL can't decode the bytes -> None before OCR is ever reached.
    monkeypatch.setattr(ocr, "ocr_image", lambda img, **kw: {
        "text": "never used", "confidence": 90.0, "languages": "eng", "script": None})
    assert image.extract(b"not an image at all") is None


def test_image_extract_ocr_none_returns_none(monkeypatch):
    # OCR unavailable (e.g. no tesseract binary) -> extract degrades to None.
    monkeypatch.setattr(ocr, "ocr_image", lambda img, **kw: None)
    assert image.extract(_make_png()) is None


def test_image_extract_ocr_empty_text_returns_none(monkeypatch):
    # A blank image reads as empty text -> no false document.
    monkeypatch.setattr(ocr, "ocr_image", lambda img, **kw: {
        "text": "  \n ", "confidence": None, "languages": "eng", "script": None})
    assert image.extract(_make_png()) is None


def test_parse_image_dispatch(monkeypatch):
    from app import documents
    monkeypatch.setattr(ocr, "ocr_image", lambda img, **kw: {
        "text": "dispatched", "confidence": 80.0, "languages": "eng", "script": "Latin"})
    res = documents.parse("image", _make_png())
    assert res is not None
    assert res["extractor"] == "image+ocr"
    assert res["markdown"] == "dispatched"
