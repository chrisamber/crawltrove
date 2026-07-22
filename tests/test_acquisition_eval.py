import json

import httpx
import pytest

from eval.acquisition import (
    Case,
    EvalError,
    Observation,
    aggregate,
    firecrawl_observation,
    load_cases,
    redact_report,
    score,
)
from eval import acquisition


def test_correctness_requires_success_and_expected_text():
    case = Case("simple", "https://example.com", "Example Domain", "html")
    assert score(case, Observation(True, "Example Domain", 1.0, 14, {})) is True
    assert score(case, Observation(True, "wrong", 1.0, 5, {})) is False
    assert score(case, Observation(False, "Example Domain", 1.0, 14, {})) is False


def test_aggregate_reports_success_median_range_and_native_cost():
    values = [
        Observation(True, "ok", 1.0, 2, {"credits": 1}),
        Observation(True, "ok", 3.0, 2, {"credits": 1}),
        Observation(False, "", 2.0, 0, {"credits": 1}),
    ]
    report = aggregate(values)
    assert report["successRate"] == pytest.approx(2 / 3)
    assert report["medianSeconds"] == 2.0
    assert report["rangeSeconds"] == [1.0, 3.0]
    assert report["nativeUsage"] == {"credits": 3}


def test_checked_in_cases_are_exactly_the_four_supported_classes():
    cases = load_cases("eval/acquisition/cases.json")
    assert [case.kind for case in cases] == ["html", "javascript", "text", "pdf"]
    assert len({case.name for case in cases}) == 4


def test_case_validation_rejects_duplicate_unknown_and_unsafe_cases(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([
        {"name": "same", "url": "https://a.test", "expectedText": "a", "kind": "html"},
        {"name": "same", "url": "https://b.test", "expectedText": "b", "kind": "javascript"},
        {"name": "text", "url": "file:///tmp/a", "expectedText": "c", "kind": "text"},
        {"name": "pdf", "url": "https://d.test", "expectedText": "d", "kind": "other"},
    ]), encoding="utf-8")
    with pytest.raises(EvalError):
        load_cases(path)


async def test_firecrawl_request_disables_cache_and_keeps_key_out_of_result():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "Example Domain", "creditsUsed": 1},
        })

    case = Case("simple", "https://example.com", "Example Domain", "html")
    async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.firecrawl.dev") as client:
        observation = await firecrawl_observation(case, client, "firecrawl-secret")
    assert observation.success is True
    assert observation.native_usage == {"credits": 1}
    assert requests[0].url.path == "/v2/scrape"
    assert json.loads(requests[0].content) == {
        "url": "https://example.com", "formats": ["markdown"],
        "maxAge": 0, "storeInCache": False,
    }
    assert requests[0].headers["authorization"] == "Bearer firecrawl-secret"
    assert "firecrawl-secret" not in repr(observation)


async def test_firecrawl_rejects_nonnumeric_native_usage():
    def handler(_request):
        return httpx.Response(200, json={
            "success": True,
            "data": {"markdown": "Example Domain", "creditsUsed": "one"},
        })

    case = Case("simple", "https://example.com", "Example Domain", "html")
    async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.firecrawl.dev") as client:
        with pytest.raises(EvalError, match="nonnumeric"):
            await firecrawl_observation(case, client, "key")


def test_reports_are_secret_safe_and_do_not_include_output_text():
    report = redact_report({
        "token": "super-secret", "authorization": "Bearer super-secret",
        "text": "private response body", "nested": {"apiKey": "super-secret"},
    }, secrets=("super-secret",))
    assert report == {
        "token": "[redacted]", "authorization": "[redacted]",
        "text": "[omitted]", "nested": {"apiKey": "[redacted]"},
    }


def test_observation_rejects_non_finite_values_and_usage():
    with pytest.raises(EvalError):
        Observation(True, "ok", float("inf"), 1, {})
    with pytest.raises(EvalError):
        Observation(True, "ok", 1.0, -1, {})
    with pytest.raises(EvalError):
        Observation(True, "ok", 1.0, 1, {"credits": "one"})


async def test_orchestration_dry_runs_then_makes_five_fresh_calls_per_pair(monkeypatch, tmp_path):
    cases = load_cases("eval/acquisition/cases.json")
    calls = {"crawltrove": [], "firecrawl": [], "crawl4ai": []}

    async def fake_preflight(*_args):
        return None

    def adapter(name):
        async def observe(case, *_args, **_kwargs):
            calls[name].append(case.name)
            return Observation(True, case.expected_text, 1.0, 1, {})
        return observe

    monkeypatch.setattr(acquisition, "preflight", fake_preflight)
    monkeypatch.setattr(acquisition, "crawltrove_observation", adapter("crawltrove"))
    monkeypatch.setattr(acquisition, "firecrawl_observation", adapter("firecrawl"))
    monkeypatch.setattr(acquisition, "crawl4ai_observation", adapter("crawl4ai"))

    report = await acquisition.run_benchmark(
        cases, crawltrove_url="http://localhost:8000", firecrawl_api_key="key",
        tmp_dir=tmp_path,
    )
    for recorded in calls.values():
        assert recorded.count("simple-html") == 6  # one dry run, then five measured calls
        assert len(recorded) == 21
    assert report["runsPerCase"] == 5
    assert "winner" not in report


async def test_measured_adapter_failure_is_reported_without_aborting(monkeypatch, tmp_path):
    cases = load_cases("eval/acquisition/cases.json")
    calls = {"count": 0}

    async def fake_preflight(*_args):
        return None

    async def observe(case, *_args, **_kwargs):
        calls["count"] += 1
        # All three dry-run calls succeed; one measured request then fails.
        if calls["count"] == 4:
            raise acquisition.AdapterUnavailable("secret response")
        return Observation(True, case.expected_text, 1.0, 1, {})

    monkeypatch.setattr(acquisition, "preflight", fake_preflight)
    monkeypatch.setattr(acquisition, "crawltrove_observation", observe)
    monkeypatch.setattr(acquisition, "firecrawl_observation", observe)
    monkeypatch.setattr(acquisition, "crawl4ai_observation", observe)

    report = await acquisition.run_benchmark(
        cases, crawltrove_url="http://localhost:8000", firecrawl_api_key="key",
        tmp_dir=tmp_path,
    )
    first = report["tools"]["crawltrove"]["cases"][0]
    assert first["successRate"] == pytest.approx(0.8)
    assert first["correctnessRate"] == pytest.approx(0.8)
