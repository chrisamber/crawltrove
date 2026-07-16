import io
import zipfile

from app import documents
from app.documents import epub


def _make_epub(title="Test Book",
               body="<h1>Chapter One</h1><p>The quick brown fox.</p>"
                    "<h2>A subsection</h2><p>Second paragraph.</p>") -> bytes:
    """Minimal valid EPUB 3: stored mimetype + container + one XHTML chapter."""
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml",
               '<?xml version="1.0"?>\n'
               '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
               '<rootfiles><rootfile full-path="content.opf" '
               'media-type="application/oebps-package+xml"/></rootfiles></container>')
    z.writestr("content.opf",
               '<?xml version="1.0"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:title>{title}</dc:title>'
               '<dc:identifier id="id">urn:uuid:1</dc:identifier>'
               '<dc:language>en</dc:language></metadata>'
               '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
               '<spine><itemref idref="c1"/></spine></package>')
    z.writestr("c1.xhtml",
               '<?xml version="1.0"?><!DOCTYPE html>'
               '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch1</title></head>'
               f'<body>{body}</body></html>')
    z.close()
    return buf.getvalue()


def test_epub_extract():
    res = epub.extract(_make_epub())
    assert res is not None
    assert res["extractor"] == "epub"
    assert res["ocr"] is None
    assert res["title"] == "Test Book"
    assert res["pages"] >= 1
    assert "Chapter One" in res["markdown"]
    assert "subsection" in res["markdown"]


def test_epub_extract_garbage_returns_none():
    assert epub.extract(b"PK\x03\x04 not really an epub") is None


def test_parse_epub_dispatch():
    res = documents.parse("epub", _make_epub())
    assert res is not None
    assert res["extractor"] == "epub"


def test_epub_rejects_archive_over_input_cap(monkeypatch):
    data = _make_epub()
    monkeypatch.setattr(epub, "MAX_ARCHIVE_BYTES", len(data) - 1)
    assert epub.extract(data) is None


def test_epub_rejects_too_many_zip_members(monkeypatch):
    monkeypatch.setattr(epub, "MAX_FILES", 3)
    assert epub.extract(_make_epub()) is None


def test_epub_rejects_excessive_expansion(monkeypatch):
    data = _make_epub(body=f"<p>{'x' * 20_000}</p>")
    monkeypatch.setattr(epub, "MAX_COMPRESSION_RATIO", 2)
    assert epub.extract(data) is None


def test_epub_rejects_oversized_content_entry(monkeypatch):
    monkeypatch.setattr(epub, "MAX_ENTRY_BYTES", 100)
    assert epub.extract(_make_epub()) is None
