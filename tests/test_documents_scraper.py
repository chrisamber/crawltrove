import io

from PIL import Image as PILImage

import app.fetch as fetch_mod
from app.documents import ocr
from app.scraper import WebScraper
from tests.test_documents_epub import _make_epub


def _fake_fetch(content, content_type):
    async def _f(url, timeout_s=20):
        return {"status": 200, "html": content.decode("latin-1", "ignore"),
                "content": content, "final_url": url, "content_type": content_type}
    return _f


async def test_epub_scrapes_end_to_end(monkeypatch):
    monkeypatch.setattr(fetch_mod, "fetch_http",
                        _fake_fetch(_make_epub(), "application/epub+zip"))
    res = await WebScraper().scrape("https://x/book.epub", engine="auto")
    assert res["success"] is True
    assert res["metadata"]["extractor"] == "epub"
    assert res["metadata"]["engine"] == "http"
    assert "ocr" not in res["metadata"]          # EPUB never sets OCR provenance
    assert "Chapter One" in res["markdown"]


async def test_image_scrapes_end_to_end(monkeypatch):
    # Tesseract is not installed here — fake the image-level OCR call.
    monkeypatch.setattr(ocr, "ocr_image", lambda img, **kw: {
        "text": "text read off the image", "confidence": 88.0,
        "languages": "eng", "script": "Latin"})
    buf = io.BytesIO()
    PILImage.new("RGB", (40, 20), "white").save(buf, format="PNG")
    monkeypatch.setattr(fetch_mod, "fetch_http",
                        _fake_fetch(buf.getvalue(), "image/png"))
    res = await WebScraper().scrape("https://x/scan.png", engine="auto")
    assert res["success"] is True
    assert res["metadata"]["extractor"] == "image+ocr"
    assert res["metadata"]["engine"] == "http"     # never escalated to Playwright
    assert res["metadata"]["ocr"]["engine"] == "tesseract"
    assert res["metadata"]["ocr"]["pages_ocred_count"] == 1
    assert "text read off the image" in res["markdown"]


async def test_unparseable_doc_degrades_without_playwright(monkeypatch):
    # Sniffs as EPUB (by content-type) but the bytes are junk -> parse() is None.
    monkeypatch.setattr(fetch_mod, "fetch_http",
                        _fake_fetch(b"PK\x03\x04 junk", "application/epub+zip"))
    res = await WebScraper().scrape("https://x/broken.epub", engine="auto")
    # Degraded on the HTTP tier — NOT rendered in a browser.
    assert res["success"] is True
    assert res["metadata"]["engine"] == "http"
    assert res["metadata"]["extractor"] != "epub"  # not the document path; fell back to HTML extraction
