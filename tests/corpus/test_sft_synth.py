from app.corpus import sft_synth


def _record(**over):
    rec = {
        "symbol": "ScrollView",
        "title": "ScrollView",
        "framework": "SwiftUI",
        "availability": {"introduced": "iOS 27.0", "deprecated": None, "beta": True},
        "swift_version": "6.4",
        "url": "https://developer.apple.com/documentation/swiftui/scrollview",
        "license_bucket": "apple-developer-docs-review-required",
        "text": "ScrollView — a scrollable view. New in iOS 27.",
    }
    rec.update(over)
    return rec


def test_introduced_pair_emitted():
    pairs = sft_synth.deterministic_pairs(_record())
    q = next(p for p in pairs if "introduced" in p["question"].lower())
    assert q["kind"] == "availability"
    assert "iOS 27.0" in q["answer"]
    assert "ScrollView" in q["question"]


def test_deprecated_pair_emitted_only_when_deprecated():
    not_dep = sft_synth.deterministic_pairs(_record())
    assert not any(p["kind"] == "deprecation" for p in not_dep)
    dep = sft_synth.deterministic_pairs(
        _record(availability={"introduced": "iOS 15.0", "deprecated": True, "beta": False}))
    assert any(p["kind"] == "deprecation" for p in dep)


def test_beta_pair_emitted_when_beta():
    pairs = sft_synth.deterministic_pairs(_record())
    assert any("beta" in p["answer"].lower() or "prerelease" in p["answer"].lower() for p in pairs)


def test_no_symbol_no_pairs():
    pairs = sft_synth.deterministic_pairs(_record(symbol="", title=""))
    assert pairs == []


async def test_conceptual_pairs_uses_extractor():
    async def fake_extractor(markdown, url, schema, prompt="", **kw):
        assert "ScrollView" in markdown  # card text passed through
        return {"data": {"pairs": [
            {"question": "How do you make a view scrollable in SwiftUI?",
             "answer": "Wrap it in a ScrollView."},
            {"question": "", "answer": "dropped — empty question"},
        ]}}
    pairs = await sft_synth.conceptual_pairs(_record(), n=2, extractor=fake_extractor)
    assert len(pairs) == 1
    assert pairs[0]["kind"] == "conceptual"


async def test_generate_pairs_combines_and_survives_llm_failure():
    async def boom(*a, **k):
        raise RuntimeError("model down")
    out = await sft_synth.generate_pairs(_record(), extractor=boom)
    assert out["llm_skipped"] is True
    # deterministic pairs still present
    assert any(p["kind"] == "availability" for p in out["pairs"])
