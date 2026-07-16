from app.corpus import schema


def _valid_record():
    return schema.new_record(
        id="chunk-1",
        source="appledocs-docc",
        url="https://developer.apple.com/documentation/swiftui/view/task(priority:_:)",
        title="View.task(priority:_:)",
        framework="SwiftUI",
        symbol="View.task(priority:_:)",
        symbol_kind="instance method",
        platforms=["iOS", "macOS"],
        availability={"introduced": "iOS 15.0", "deprecated": None, "beta": False},
        swift_version="6.4",
        xcode_version="27",
        scraped_at="2026-06-17",
        license_bucket="apple-developer-docs-review-required",
        content_hash="sha256:abc",
        chunk_type="symbol_card",
        namespace="apple-framework",
        text="A symbol discussion.",
    )


def test_valid_record_has_no_errors():
    assert schema.validate_record(_valid_record()) == []


def test_missing_required_field_reported():
    rec = _valid_record()
    del rec["url"]
    errors = schema.validate_record(rec)
    assert any("url" in e for e in errors)


def test_empty_url_is_an_error():
    rec = _valid_record()
    rec["url"] = ""
    assert any("url" in e for e in schema.validate_record(rec))


def test_bad_chunk_type_reported():
    rec = _valid_record()
    rec["chunk_type"] = "page_blob"
    assert any("chunk_type" in e for e in schema.validate_record(rec))


def test_bad_namespace_reported():
    rec = _valid_record()
    rec["namespace"] = "nonsense"
    assert any("namespace" in e for e in schema.validate_record(rec))


def test_new_record_fills_defaults():
    rec = schema.new_record(id="x")
    for field in schema.REQUIRED_FIELDS:
        assert field in rec
