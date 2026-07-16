"""Corpus-side guard: the Apple-source license pin is a legal guardrail.

A page-level CC marker is NOT proof the underlying Apple content is freely
licensed, so an Apple source must never be upgraded by a detected marker.
"""
from app.corpus import license_buckets as lb


def test_apple_sources_are_never_upgraded_by_a_cc_marker():
    assert lb.bucket_for("appledocs-docc", "CC-BY-4.0") == "apple-developer-docs-review-required"
    assert lb.bucket_for("appledocs-docc", "CC0-1.0") == "apple-developer-docs-review-required"
    assert lb.bucket_for("wwdc", "CC-BY-4.0") == "apple-developer-docs-review-required"
    assert lb.bucket_for("samplecode", "CC-BY-4.0") == "apple-sample-code-review-required"


def test_non_apple_unknown_source_may_upgrade_from_a_marker():
    # The pin is Apple-specific; an otherwise-unknown source CAN be upgraded.
    assert lb.bucket_for("some-random-blog", "CC-BY-4.0") == "cc-by-4.0"
    assert lb.bucket_for("some-random-blog") == "unknown"
