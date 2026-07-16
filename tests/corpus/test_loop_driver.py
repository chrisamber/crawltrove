import importlib

loop = importlib.import_module("scripts.scrape_swift64_loop")


def test_pending_filters_done():
    m = {"batches": [{"id": "a", "source": "wwdc", "version_hint": "x"},
                     {"id": "b", "source": "wwdc", "version_hint": "x"}]}
    state = {"done": {"a": {"status": "ok"}}}
    pend = loop.pending(m, state)
    assert [b["id"] for b in pend] == ["b"]


def test_scraper_argv_appledocs():
    argv = loop.scraper_argv({"id": "x", "source": "appledocs-docc",
                              "root": "https://developer.apple.com/documentation/swiftui",
                              "max_pages": 1200})
    assert "scripts/scrape_apple_docs.py" in argv[0]
    assert "--root" in argv
    assert "--max-pages" in argv and "1200" in argv


def test_scraper_argv_evolution_and_wwdc():
    assert "scrape_swift_evolution.py" in loop.scraper_argv({"source": "swift-evolution"})[0]
    wwdc = loop.scraper_argv({"source": "wwdc", "limit": 5})
    assert "scrape_wwdc_transcripts.py" in wwdc[0]
    assert "--limit" in wwdc and "5" in wwdc


def test_scraper_argv_wwdc_topic_batch_passes_keywords_and_framework():
    argv = loop.scraper_argv({
        "id": "wwdc-mapkit", "source": "wwdc",
        "keywords": "mapkit,mkmapview", "framework": "mapkit",
    })
    assert "--keywords" in argv and "mapkit,mkmapview" in argv
    assert "--default-framework" in argv and "mapkit" in argv
    assert "--out" in argv
    out = argv[argv.index("--out") + 1]
    assert out.endswith("wwdc-mapkit.jsonl")


def test_scraper_argv_web_is_none():
    assert loop.scraper_argv({"source": "web", "urls": ["https://x"]}) is None


def test_flat_jsonl_for_wwdc_keys_by_batch_id():
    # Two wwdc-sourced batches must never share an output file, or a
    # concurrent drain run would clobber one another's records.
    default_path = loop._flat_jsonl_for({"source": "wwdc", "id": "wwdc-2026"})
    mapkit_path = loop._flat_jsonl_for({"source": "wwdc", "id": "wwdc-mapkit"})
    assert default_path != mapkit_path
    assert default_path.endswith("wwdc-2026.jsonl")
    assert mapkit_path.endswith("wwdc-mapkit.jsonl")


def test_flat_jsonl_for_swift_evolution_unaffected():
    assert loop._flat_jsonl_for({"source": "swift-evolution"}).endswith(
        "swift-evolution-proposals.jsonl"
    )


def test_state_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    loop.save_state(p, {"done": {"a": {"status": "ok"}}})
    assert loop.load_state(p)["done"]["a"]["status"] == "ok"
    assert loop.load_state(tmp_path / "missing.json") == {"done": {}}
