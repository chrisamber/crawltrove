import json
import math
import importlib

import httpx
import pytest

from eval import retrieval as retrieval_eval


def _hit(ref, *, parent=None, url=None, kind="corpus"):
    return {
        "kind": kind, "ref": ref, "url": url,
        "parentId": parent or f"{kind}:ref:{ref}",
    }


def test_metrics_score_parent_level_binary_relevance():
    hits = [
        _hit("noise"),
        _hit("a", parent="corpus:hash:a"),
        _hit("a-duplicate", parent="corpus:hash:a"),
        _hit("b", url="https://example.test/b"),
    ]
    score = retrieval_eval.score_case(
        ["corpus:hash:a", "corpus:url:https://example.test/b"], hits, 4)
    ideal = 1.0 + 1.0 / math.log2(3)
    actual = 1.0 / math.log2(3) + 1.0 / math.log2(5)
    assert score["recall"] == 1.0
    assert score["mrr"] == 0.5
    assert score["ndcg"] == pytest.approx(actual / ideal)
    assert score["firstRelevantRank"] == 2


def test_no_hits_score_zero_and_duplicate_results_do_not_multiply_gain():
    empty = retrieval_eval.score_case(["corpus:hash:a"], [], 10)
    assert (empty["recall"], empty["mrr"], empty["ndcg"]) == (0.0, 0.0, 0.0)
    duplicate = _hit("a", parent="corpus:hash:a")
    scored = retrieval_eval.score_case(
        ["corpus:hash:a"], [duplicate, duplicate], 10)
    assert (scored["recall"], scored["mrr"], scored["ndcg"]) == (1.0, 1.0, 1.0)


def test_loader_is_deterministic_and_rejects_invalid_cases(tmp_path):
    good = {
        "name": "b", "query": "query", "relevantIds": ["corpus:ref:b"],
    }
    (tmp_path / "b.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "a.json").write_text(json.dumps({**good, "name": "a"}), encoding="utf-8")
    assert [case["name"] for case in retrieval_eval.load_cases(str(tmp_path))] == ["a", "b"]
    (tmp_path / "a.json").write_text(json.dumps({"name": "bad"}), encoding="utf-8")
    with pytest.raises(retrieval_eval.EvalError, match="query"):
        retrieval_eval.load_cases(str(tmp_path))


async def test_evaluate_mode_uses_hybrid_api_and_parent_identities():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [
            _hit("chunk", parent="corpus:hash:parent")
        ]})

    cases = [{
        "name": "case", "query": "actors",
        "relevantIds": ["corpus:hash:parent"],
        "tags": ["exact-symbol"],
        "filters": {"kind": "corpus", "namespace": "swift-language"},
    }]
    async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://test") as client:
        report = await retrieval_eval.evaluate_mode(cases, "hybrid", 10, client)
    assert report["aggregate"] == {"recall": 1.0, "mrr": 1.0, "ndcg": 1.0}
    assert report["exactSymbol"] == report["aggregate"]
    assert requests[0].url.path == "/api/search/hybrid"
    assert requests[0].url.params["namespace"] == "swift-language"
    assert requests[0].url.params["mode"] == "hybrid"


def test_coverage_and_gate_are_explicit():
    cases = [{"relevantIds": ["corpus:url:https://example.test/a"]}]
    assert retrieval_eval.unresolved_ids(cases, set()) == [
        "corpus:url:https://example.test/a"]
    filtered = retrieval_eval.unresolved_case_ids(
        [{"name": "case", "relevantIds": ["corpus:ref:a"],
          "filters": {"namespace": "swift-language"}}],
        lambda filters: {"corpus:ref:a"} if not filters else set())
    assert filtered == ["case: corpus:ref:a"]
    semantic = {
        "aggregate": {"recall": 1.0, "mrr": 1.0, "ndcg": 1.0},
        "exactSymbol": {"recall": 1.0, "mrr": 1.0, "ndcg": 1.0},
    }
    hybrid = {
        "aggregate": {"recall": 1.0, "mrr": .9, "ndcg": 1.0},
        "exactSymbol": {"recall": 1.0, "mrr": 1.0, "ndcg": .8},
    }
    reasons = retrieval_eval.gate_reports({"semantic": semantic, "hybrid": hybrid})
    assert reasons == [
        "hybrid aggregate mrr regressed",
        "hybrid exact-symbol ndcg regressed",
    ]


def test_checked_in_cases_are_valid():
    cases = retrieval_eval.load_cases("eval/retrieval/cases")
    assert len(cases) == 6
    assert sum("exact-symbol" in case.get("tags", []) for case in cases) == 3


def test_cli_propagates_configured_api_key_without_logging_it(monkeypatch):
    cli = importlib.import_module("eval.retrieval.__main__")
    monkeypatch.setattr(cli, "API_KEYS", {"secret-b", "secret-a"})
    monkeypatch.setattr(cli, "APP_PASSWORD", "password")
    options = cli._client_security()
    assert options == {"headers": {"x-api-key": "secret-a"}}
