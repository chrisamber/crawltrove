from app.corpus import manifest


def _good():
    return {
        "swift_version": "6.4",
        "ios_version": "27",
        "sft_include_restricted": True,
        "batches": [
            {"id": "apple-swiftui", "source": "appledocs-docc",
             "root": "https://developer.apple.com/documentation/swiftui",
             "version_hint": "iOS 27"},
            {"id": "swift-evolution", "source": "swift-evolution",
             "version_hint": "Swift 6.4"},
            {"id": "notes", "source": "web", "corpus_source": "swift-book",
             "urls": ["https://www.swift.org/blog/"], "version_hint": "6.4"},
        ],
    }


def test_valid_manifest_has_no_errors():
    assert manifest.validate_manifest(_good()) == []


def test_unknown_source_reported():
    m = _good()
    m["batches"][0]["source"] = "ftp"
    assert any("source" in e for e in manifest.validate_manifest(m))


def test_appledocs_requires_root():
    m = _good()
    del m["batches"][0]["root"]
    assert any("root" in e for e in manifest.validate_manifest(m))


def test_web_requires_urls():
    m = _good()
    del m["batches"][2]["urls"]
    assert any("urls" in e for e in manifest.validate_manifest(m))


def test_missing_version_hint_reported():
    m = _good()
    del m["batches"][1]["version_hint"]
    assert any("version_hint" in e for e in manifest.validate_manifest(m))


def test_corpus_source_falls_back_to_source():
    assert manifest.corpus_source({"source": "wwdc"}) == "wwdc"
    assert manifest.corpus_source({"source": "web", "corpus_source": "swift-book"}) == "swift-book"


def test_load_manifest_reads_yaml(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("swift_version: '6.4'\nbatches: []\n", encoding="utf-8")
    m = manifest.load_manifest(p)
    assert m["swift_version"] == "6.4"
    assert m["batches"] == []
