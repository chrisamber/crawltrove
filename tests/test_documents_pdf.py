from app import documents
from app.documents import pdf
from tests.pdf_fixture import make_pdf


def test_pdf_extract_text_layer():
    res = pdf.extract(make_pdf("The quick brown fox jumps over the lazy dog.",
                               title="Guard Fixture"))
    assert res is not None
    assert set(res.keys()) == {"markdown", "title", "pages", "ocr", "parts", "extractor"}
    assert "quick brown fox" in res["markdown"]
    assert res["title"] == "Guard Fixture"
    assert res["pages"] == 1
    assert res["ocr"] is None  # text-layer page → OCR never runs


def test_pdf_extract_garbage_returns_none():
    assert pdf.extract(b"not a pdf at all") is None


def test_pdf_extract_sets_extractor():
    res = pdf.extract(make_pdf("The quick brown fox jumps over the lazy dog."))
    assert res["extractor"] == "pdf"  # text-layer → no OCR → plain "pdf"


def test_parse_pdf_dispatch():
    res = documents.parse("pdf", make_pdf("The quick brown fox jumps over the lazy dog."))
    assert res is not None
    assert res["extractor"] == "pdf"
    assert "quick brown fox" in res["markdown"]


def test_parse_unknown_kind_returns_none():
    assert documents.parse("epub", b"") is None  # no EPUB parser yet (Task 5)
