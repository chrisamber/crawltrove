from app.corpus import router


def test_apple_docs_go_to_rag_only():
    rec = {"license_bucket": "apple-developer-docs-review-required"}
    assert router.route(rec) == {"rag"}


def test_apple_docs_never_sft_or_dapt():
    rec = {"license_bucket": "apple-developer-docs-review-required"}
    targets = router.route(rec)
    assert "sft" not in targets and "dapt" not in targets


def test_sample_code_rag_only_unless_permissive_repo_license():
    rec = {"license_bucket": "apple-sample-code-review-required"}
    assert router.route(rec) == {"rag"}
    rec2 = {"license_bucket": "apple-sample-code-review-required", "repo_license": "MIT"}
    assert router.route(rec2) == {"rag", "dapt"}


def test_swift_org_permissive_rag_and_dapt():
    rec = {"license_bucket": "swift-org-permissive"}
    assert router.route(rec) == {"rag", "dapt"}


def test_cc_by_rag_and_dapt_but_not_sft():
    rec = {"license_bucket": "cc-by-4.0"}
    targets = router.route(rec)
    assert targets == {"rag", "dapt"}
    assert "sft" not in targets


def test_own_content_all_three():
    rec = {"license_bucket": "own-content"}
    assert router.route(rec) == {"rag", "sft", "dapt"}


def test_unknown_bucket_routes_nowhere():
    assert router.route({"license_bucket": "unknown"}) == set()
