"""Unit tests for the consolidated row mappers in app/normalize.py.

Pure functions, no DB. They lift the URL normalization + page-row mapping that
were inlined across runner.py and crawler.py, and add the
extract->records mapping. Every function must be total: never raise, even on
junk input.
"""
from app import normalize


# --- normalize_url -----------------------------------------------------------

def test_normalize_url_strips_trailing_slash_and_fragment_and_lowercases_host():
    assert (normalize.normalize_url("HTTPS://Example.COM/Path/#frag")
            == "https://example.com/Path")


def test_normalize_url_preserves_query_as_page_identity():
    assert (normalize.normalize_url("https://e.com/search?q=2&p=1")
            == "https://e.com/search?q=2&p=1")


def test_normalize_url_root_has_no_trailing_slash():
    assert normalize.normalize_url("https://e.com/") == "https://e.com"


def test_normalize_url_never_raises_on_junk():
    # No scheme/host, empty, None-ish — must return a string, not explode.
    for bad in ("", "not a url", "::::", "/relative/only"):
        out = normalize.normalize_url(bad)
        assert isinstance(out, str)


def test_normalize_url_matches_crawler_definition():
    """The crawler must delegate to the shared definition (single source)."""
    from app.crawler import WebCrawler
    c = WebCrawler()
    for u in ("https://A.com/x/", "http://b.com/y?z=1#f", "https://c.com"):
        assert c._normalize_url(u) == normalize.normalize_url(u)


# --- page_row_from_result (scrape shape) -------------------------------------

def _scrape_result():
    return {
        "success": True,
        "url": "https://example.com/song",
        "markdown": "# hello",
        "metadata": {
            "url": "https://example.com/song",
            "engine": "http",
            "extractor": "trafilatura",
            "status_code": 200,
            "license": {"id": "MIT"},
            "dedup": {"content_hash": "deadbeef"},
        },
    }


def test_page_row_from_result_maps_columns_and_paths():
    row = normalize.page_row_from_result(
        _scrape_result(), "stem1",
        raw_html_path="data/runs/stem1/page-1.html.txt",
        screenshot_path="data/runs/stem1/page-1.png",
    )
    assert row["url"] == "https://example.com/song"
    assert row["status_code"] == 200            # promoted from metadata
    assert row["engine"] == "http"
    assert row["extractor"] == "trafilatura"
    assert row["content_hash"] == "deadbeef"    # promoted from metadata.dedup
    assert row["extracted_text"] == "# hello"
    assert row["raw_json_path"] == "data/scrapes/stem1.json"
    assert row["raw_md_path"] == "data/scrapes/stem1.md"
    assert row["raw_html_path"] == "data/runs/stem1/page-1.html.txt"
    # screenshot path rides in metadata (no dedicated column)
    assert row["metadata"]["screenshot_path"] == "data/runs/stem1/page-1.png"
    assert row["metadata"]["license"]["id"] == "MIT"


def test_page_row_from_result_does_not_mutate_caller_metadata():
    result = _scrape_result()
    normalize.page_row_from_result(result, "stem1",
                                   screenshot_path="data/runs/stem1/page-1.png")
    assert "screenshot_path" not in result["metadata"]


def test_page_row_from_result_no_stem_yields_null_paths():
    row = normalize.page_row_from_result(_scrape_result(), None)
    assert row["raw_json_path"] is None
    assert row["raw_md_path"] is None
    assert row["raw_html_path"] is None


# --- page_row_from_crawl_item (flattened crawl shape) ------------------------

def test_page_row_from_crawl_item_rebuilds_metadata_and_promotes_hash():
    item = {
        "url": "https://e.com/a",
        "title": "A",
        "description": "d",
        "engine": "browser",
        "extractor": "trafilatura",
        "license": {"id": "CC-BY-4.0"},
        "quality": {"score": 1.0},
        "language": "en",
        "status_code": 403,
        "markdown": "body",
        "dedup": {"content_hash": "h1"},
    }
    row = normalize.page_row_from_crawl_item(item)
    assert row["url"] == "https://e.com/a"
    assert row["status_code"] == 403
    assert row["engine"] == "browser"
    assert row["content_hash"] == "h1"
    assert row["extracted_text"] == "body"
    assert row["metadata"]["title"] == "A"
    assert row["metadata"]["license"]["id"] == "CC-BY-4.0"
    assert row["metadata"]["status_code"] == 403
    assert row["metadata"]["dedup"]["content_hash"] == "h1"


# --- record_rows_from_extract ------------------------------------------------

def test_record_rows_from_extract_single_object():
    extracted = {"data": {"artist": "X", "work": "Y"}, "model": "m", "usage": {}}
    rows = normalize.record_rows_from_extract(extracted, "https://e.com")
    assert len(rows) == 1
    assert rows[0]["source_url"] == "https://e.com"
    assert rows[0]["record_type"] == "extract"
    assert rows[0]["data_json"] == {"artist": "X", "work": "Y"}
    assert rows[0]["content_hash"] is None      # caller fills it
    assert rows[0]["confidence"] is None


def test_record_rows_from_extract_list_one_row_each():
    extracted = {"data": [{"a": 1}, {"a": 2, "confidence": 0.9}]}
    rows = normalize.record_rows_from_extract(extracted, "https://e.com")
    assert len(rows) == 2
    assert rows[0]["data_json"] == {"a": 1}
    assert rows[1]["data_json"] == {"a": 2, "confidence": 0.9}
    assert rows[1]["confidence"] == 0.9          # lifted from the record body


def test_record_rows_from_extract_none_or_missing_is_empty():
    assert normalize.record_rows_from_extract({}, "https://e.com") == []
    assert normalize.record_rows_from_extract({"data": None}, "https://e.com") == []
    assert normalize.record_rows_from_extract(None, "https://e.com") == []
