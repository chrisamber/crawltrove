import importlib
import json

bc = importlib.import_module("scripts.build_corpus")


def _apple_result(url, title, ch, platforms=None):
    return {
        "url": url,
        "markdown": f"# {title}\n\nDiscussion of {title}.",
        "metadata": {
            "title": title,
            "symbolKind": "class",
            "roleHeading": "Class",
            "platforms": platforms or [{"name": "iOS", "introducedAt": "27.0"}],
            "dedup": {"content_hash": ch},
        },
    }


def test_availability_from_platforms():
    av = bc.availability_from_platforms([{"name": "iOS", "introducedAt": "27.0"},
                                         {"name": "macOS", "introducedAt": None}])
    assert av == {"introduced": "iOS 27.0", "deprecated": None, "beta": False}
    assert bc.availability_from_platforms(None) == {"introduced": None, "deprecated": None, "beta": False}
    assert bc.availability_from_platforms([{"name": "iOS", "introducedAt": None}])["introduced"] is None


def test_framework_from_url():
    assert bc.framework_from_url("https://developer.apple.com/documentation/swiftui/view") == "swiftui"
    assert bc.framework_from_url("https://example.com/x") == ""


def test_content_hash_for_prefers_dedup_then_falls_back():
    assert bc.content_hash_for(_apple_result("u", "t", "abc123")) == "abc123"
    r = _apple_result("u", "t", "")
    r["metadata"]["dedup"] = {}
    assert bc.content_hash_for(r).startswith("sha256:")


def test_build_record_fields_and_validates():
    from app.corpus import schema
    rec = bc.build_record(
        _apple_result("https://developer.apple.com/documentation/swiftui/view", "View", "h1"),
        "appledocs-docc", scraped_at="2026-06-17")
    assert rec["license_bucket"] == "apple-developer-docs-review-required"
    assert rec["namespace"] == "apple-framework"
    assert rec["framework"] == "swiftui"
    assert rec["symbol"] == "View"
    assert rec["availability"]["introduced"] == "iOS 27.0"
    assert rec["url"].endswith("/swiftui/view")
    assert rec["text"].startswith("# View")
    assert schema.validate_record(rec) == []


def test_write_records_apple_rag_only_and_idempotent(tmp_path):
    recs = [bc.build_record(
        _apple_result("https://developer.apple.com/documentation/swiftui/view", "View", "h1"),
        "appledocs-docc")]
    first = bc.write_records(recs, tmp_path, "appledocs-docc")
    assert first["written"] == {"rag": 1}
    assert first["unchanged"] == 0
    rag_file = tmp_path / "corpus" / "rag" / "apple-framework" / "swiftui.jsonl"
    assert rag_file.exists()
    assert not any((tmp_path / "corpus" / "sft").rglob("*.jsonl"))
    assert not any((tmp_path / "corpus" / "dapt").rglob("*.jsonl"))
    # one line, valid json
    line = rag_file.read_text(encoding="utf-8").strip()
    assert json.loads(line)["symbol"] == "View"
    # idempotent second run
    second = bc.write_records(recs, tmp_path, "appledocs-docc")
    assert second["unchanged"] == 1
    assert second["written"] == {}
    assert len(rag_file.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_records_from_crawl(tmp_path):
    job = {"source": "appledocs-docc", "results": [
        _apple_result("https://developer.apple.com/documentation/swiftui/view", "View", "h1"),
        _apple_result("https://developer.apple.com/documentation/swiftui/text", "Text", "h2"),
    ]}
    p = tmp_path / "job.json"
    p.write_text(json.dumps(job), encoding="utf-8")
    recs = bc.records_from_crawl(p, "appledocs-docc")
    assert len(recs) == 2
    assert {r["symbol"] for r in recs} == {"View", "Text"}


def test_write_records_counts_no_target(tmp_path):
    # an unknown-source record routes nowhere
    rec = bc.build_record(
        {"url": "https://example.com/x", "markdown": "# X\n\nbody.",
         "metadata": {"title": "X", "dedup": {"content_hash": "hz"}}},
        "some-blog")
    stats = bc.write_records([rec], tmp_path, "some-blog")
    assert stats["written"] == {}
    assert stats["no_target"] == 1
