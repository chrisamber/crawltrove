import importlib
import json

gs = importlib.import_module("scripts.generate_sft")


def _rec(h, bucket="apple-developer-docs-review-required", **over):
    rec = {
        "content_hash": h, "symbol": "ScrollView", "title": "ScrollView",
        "framework": "SwiftUI", "swift_version": "6.4",
        "url": "https://developer.apple.com/documentation/swiftui/scrollview",
        "license_bucket": bucket, "text": "A scrollable view.",
        "availability": {"introduced": "iOS 27.0", "deprecated": None, "beta": False},
    }
    rec.update(over)
    return rec


def test_to_sft_record_shape():
    out = gs.to_sft_record(_rec("h1"), {"kind": "conceptual",
                                        "question": "Q?", "answer": "A."})
    assert out["sft_origin"] == "synthetic"
    assert out["source_url"].startswith("https://developer.apple.com")
    assert out["license_bucket"] == "apple-developer-docs-review-required"
    assert out["messages"][0] == {"role": "user", "content": "Q?"}
    assert out["messages"][1] == {"role": "assistant", "content": "A."}


def test_eligible_respects_flag():
    apple = _rec("h2", bucket="apple-developer-docs-review-required")
    perm = _rec("h3", bucket="swift-org-permissive")
    assert gs.eligible(apple, include_restricted=True) is True
    assert gs.eligible(apple, include_restricted=False) is False
    assert gs.eligible(perm, include_restricted=False) is True


async def test_run_is_idempotent(tmp_path):
    rag = tmp_path / "rag" / "apple-framework"
    rag.mkdir(parents=True)
    (rag / "SwiftUI.jsonl").write_text(json.dumps(_rec("hA")) + "\n", encoding="utf-8")
    out = tmp_path / "sft"
    state = tmp_path / ".sft_state.json"

    async def fake(record, *, n=2, extractor=None):
        return {"pairs": [{"kind": "conceptual", "question": "Q?", "answer": "A."}],
                "llm_skipped": False}

    # patch generate_pairs and restore after test
    _orig = gs.sft_synth.generate_pairs
    gs.sft_synth.generate_pairs = fake
    try:
        first = await gs.run(tmp_path / "rag", out, include_restricted=True,
                             n=2, limit=0, state_path=state)
        assert first["records"] == 1 and first["pairs"] == 1
        second = await gs.run(tmp_path / "rag", out, include_restricted=True,
                              n=2, limit=0, state_path=state)
        assert second["records"] == 0 and second["skipped_seen"] == 1
        assert (out / "_PROVENANCE.md").exists()
    finally:
        gs.sft_synth.generate_pairs = _orig


async def test_run_counts_llm_skipped(tmp_path):
    rag = tmp_path / "rag" / "apple-framework"
    rag.mkdir(parents=True)
    (rag / "SwiftUI.jsonl").write_text(json.dumps(_rec("hB")) + "\n", encoding="utf-8")
    out = tmp_path / "sft"
    state = tmp_path / ".sft_state.json"

    async def fake(record, *, n=2, extractor=None):
        return {"pairs": [{"kind": "conceptual", "question": "Q?", "answer": "A."}],
                "llm_skipped": True}

    # patch generate_pairs and restore after test
    _orig = gs.sft_synth.generate_pairs
    gs.sft_synth.generate_pairs = fake
    try:
        stats = await gs.run(tmp_path / "rag", out, include_restricted=True,
                             n=2, limit=0, state_path=state)
        assert stats["llm_skipped"] == 1
        assert stats["records"] == 1
        assert stats["pairs"] == 1
    finally:
        gs.sft_synth.generate_pairs = _orig
