import json

from app.corpus import crawl_shape


def test_result_from_flat_meta_url():
    rec = {"text": "Proposal body", "meta": {"url": "https://x/se-0001", "title": "SE-0001"}}
    out = crawl_shape.result_from_flat(rec, "swift-evolution")
    assert out["url"] == "https://x/se-0001"
    assert out["markdown"] == "Proposal body"
    assert out["metadata"]["title"] == "SE-0001"
    assert out["metadata"]["source"] == "swift-evolution"


def test_result_from_flat_top_level_url():
    rec = {"text": "Transcript", "url": "https://x/wwdc/101", "framework": "SwiftUI"}
    out = crawl_shape.result_from_flat(rec, "wwdc")
    assert out["url"] == "https://x/wwdc/101"
    assert out["metadata"]["framework"] == "SwiftUI"


def test_result_from_flat_drops_unusable():
    assert crawl_shape.result_from_flat({"text": "", "url": ""}, "wwdc") is None
    assert crawl_shape.result_from_flat({"meta": {"url": "https://x"}}, "x") is None  # no text


def test_job_from_results_shape():
    results = [{"url": "https://x/1", "markdown": "a", "metadata": {}}]
    job = crawl_shape.job_from_results(results, base_url="https://x", source="wwdc", now="2026-06-17")
    assert job["status"] == "completed"
    assert job["source"] == "wwdc"
    assert job["total"] == 1
    assert job["results"] == results


def test_read_flat_jsonl(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text(
        json.dumps({"text": "t1", "meta": {"url": "https://x/1"}}) + "\n"
        + json.dumps({"text": "", "meta": {"url": ""}}) + "\n",  # dropped
        encoding="utf-8",
    )
    rows = crawl_shape.read_flat_jsonl(p, "swift-evolution")
    assert len(rows) == 1
    assert rows[0]["url"] == "https://x/1"
