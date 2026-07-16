from app.corpus import license_buckets as lb


def test_apple_docs_always_review_required_even_with_cc_marker():
    assert lb.bucket_for("appledocs-docc") == "apple-developer-docs-review-required"
    # a detected CC marker on an Apple page must NOT upgrade it
    assert lb.bucket_for("appledocs-docc", "CC-BY-4.0") == "apple-developer-docs-review-required"


def test_wwdc_is_apple_review_required():
    assert lb.bucket_for("wwdc") == "apple-developer-docs-review-required"


def test_sample_code_is_its_own_review_bucket():
    assert lb.bucket_for("samplecode") == "apple-sample-code-review-required"


def test_swift_evolution_is_permissive():
    assert lb.bucket_for("swift-evolution") == "swift-org-permissive"


def test_swift_book_is_cc_by():
    assert lb.bucket_for("swift-book") == "cc-by-4.0"


def test_own_content():
    assert lb.bucket_for("own") == "own-content"


def test_unknown_source_upgraded_by_cc_license():
    assert lb.bucket_for("some-blog") == "unknown"
    assert lb.bucket_for("some-blog", "CC-BY-4.0") == "cc-by-4.0"
    assert lb.bucket_for("some-blog", "CC0-1.0") == "cc0-1.0"


def test_every_mapped_bucket_is_known():
    for bucket in lb.SOURCE_BUCKET.values():
        assert bucket in lb.ALL_BUCKETS
