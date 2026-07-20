"""Tests for quality tiering and tier-aware routing."""
from app.corpus import quality_tiers as qt
from app.corpus import router


def test_tier_from_failure_count():
    assert qt.tier_for({"failures": []}) == "high"
    assert qt.tier_for({"failures": ["word_count"]}) == "medium"
    assert qt.tier_for({"failures": ["a", "b"]}) == "medium"
    assert qt.tier_for({"failures": ["a", "b", "c"]}) == "low"


def test_no_report_is_medium():
    assert qt.tier_for(None) == "medium"
    assert qt.tier_for({}) == "medium"


def test_passed_bool_fallback():
    assert qt.tier_for({"passed": True}) == "high"
    assert qt.tier_for({"passed": False}) == "low"


def test_thresholds_overridable_via_args():
    q = {"failures": ["a", "b"]}
    # tighten so 2 failures is now 'low'
    assert qt.tier_for(q, high_max_failures=0, medium_max_failures=1) == "low"
    # loosen so 2 failures is 'high'
    assert qt.tier_for(q, high_max_failures=3) == "high"


def test_thresholds_overridable_via_env(monkeypatch):
    monkeypatch.setenv("CORPUS_TIER_MEDIUM_MAX_FAILURES", "5")
    assert qt.tier_for({"failures": ["a", "b", "c", "d"]}) == "medium"


# --- router tier-awareness -------------------------------------------------
def test_low_tier_is_rag_only_by_default():
    rec = {"license_bucket": "cc-by-4.0", "quality_tier": "low"}
    assert router.route(rec) == {"rag"}  # dapt dropped


def test_high_and_medium_tiers_keep_full_routing():
    for tier in ("high", "medium"):
        rec = {"license_bucket": "cc-by-4.0", "quality_tier": tier}
        assert router.route(rec) == {"rag", "dapt"}


def test_low_tier_own_content_drops_sft_and_dapt():
    rec = {"license_bucket": "own-content", "quality_tier": "low"}
    assert router.route(rec) == {"rag"}


def test_low_tier_can_be_kept_in_training_when_configured():
    rec = {"license_bucket": "cc-by-4.0", "quality_tier": "low"}
    assert router.route(rec, low_tier_rag_only=False) == {"rag", "dapt"}


def test_untiered_record_is_unchanged():
    # Legacy records with no quality_tier route exactly as before.
    assert router.route({"license_bucket": "cc-by-4.0"}) == {"rag", "dapt"}


def test_low_tier_env_toggle(monkeypatch):
    monkeypatch.setenv("CORPUS_LOW_TIER_RAG_ONLY", "false")
    rec = {"license_bucket": "cc-by-4.0", "quality_tier": "low"}
    assert router.route(rec) == {"rag", "dapt"}


# --- end-to-end through build_corpus --------------------------------------
def test_build_corpus_stamps_tier_and_keeps_low_out_of_dapt(tmp_path):
    import importlib
    bc = importlib.import_module("scripts.build_corpus")
    low_page = {
        "url": "https://swift.org/blog/post",
        "markdown": "# Post\n\n" + "body words here " * 40,
        "metadata": {"title": "Post", "dedup": {"content_hash": "low1"},
                     "quality": {"failures": ["a", "b", "c"], "passed": False}},
    }
    # swift-evolution -> swift-org-permissive -> {rag, dapt}; low tier -> RAG only.
    rec = bc.build_record(low_page, "swift-evolution")
    assert rec["quality_tier"] == "low"
    stats = bc.write_records([rec], tmp_path, "swift-evolution")
    assert stats["written"].get("rag", 0) >= 1
    assert "dapt" not in stats["written"]
    assert stats["by_tier"].get("low", 0) >= 1


def test_build_corpus_high_tier_reaches_dapt(tmp_path):
    import importlib
    bc = importlib.import_module("scripts.build_corpus")
    good_page = {
        "url": "https://swift.org/blog/good",
        "markdown": "# Good\n\n" + "clean body words here " * 40,
        "metadata": {"title": "Good", "dedup": {"content_hash": "good1"},
                     "quality": {"failures": [], "passed": True}},
    }
    rec = bc.build_record(good_page, "swift-evolution")
    assert rec["quality_tier"] == "high"
    stats = bc.write_records([rec], tmp_path, "swift-evolution")
    assert stats["written"].get("rag", 0) >= 1
    assert stats["written"].get("dapt", 0) >= 1
