import importlib
import json

sad = importlib.import_module("scripts.scrape_apple_docs")


def test_write_artifact_to_explicit_out(tmp_path):
    job = {"results": [{"url": "u", "markdown": "m"}], "source": "appledocs-docc"}
    out = tmp_path / "crawls" / "apple-swiftui-X.json"
    path = sad.write_artifact(job, str(out))
    assert path == str(out)
    assert json.loads(out.read_text(encoding="utf-8")) == job


def test_write_artifact_falls_back_to_save_crawl(tmp_path, monkeypatch):
    import app.storage as storage_mod
    monkeypatch.setattr(storage_mod, "save_crawl", lambda job: "STEM")
    monkeypatch.setattr(storage_mod, "CRAWLS_DIR", str(tmp_path))
    path = sad.write_artifact({"results": []}, "")
    assert path == str(tmp_path / "STEM.json")
