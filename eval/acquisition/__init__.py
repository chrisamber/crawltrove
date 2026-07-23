"""Reproducible, report-only acquisition comparison helpers.

The package intentionally keeps provider responses out of reports.  It records
only request outcomes, timings, byte counts, and native provider units.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from app.url_safety import UnsafeUrlError, ensure_public_url


CASE_KINDS = ("html", "javascript", "text", "pdf")
RUNS_PER_CASE = 5
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|api[_-]?key|token|secret|password|"
    r"markdown|html|body|raw|text)", re.IGNORECASE)
_BODY_KEY = re.compile(r"(?:markdown|html|body|raw|text)", re.IGNORECASE)


class EvalError(RuntimeError):
    """Raised when an acquisition evaluation cannot produce a valid report."""


class AdapterUnavailable(EvalError):
    """Raised when a direct acquisition adapter cannot make a request."""


@dataclass(frozen=True)
class Case:
    name: str
    url: str
    expected_text: str
    kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise EvalError("case name must be a non-empty string")
        parsed = urlsplit(self.url) if isinstance(self.url, str) else None
        if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise EvalError(f"{self.name}: URL must be absolute HTTP(S)")
        if not isinstance(self.expected_text, str) or not self.expected_text.strip():
            raise EvalError(f"{self.name}: expected text must be non-empty")
        if self.kind not in CASE_KINDS:
            raise EvalError(f"{self.name}: unknown case type {self.kind!r}")


@dataclass(frozen=True)
class Observation:
    success: bool
    text: str
    duration_seconds: float
    output_bytes: int
    native_usage: Mapping[str, float]

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise EvalError("observation success must be a boolean")
        if not isinstance(self.text, str):
            raise EvalError("observation text must be a string")
        if (not isinstance(self.duration_seconds, (int, float))
                or not math.isfinite(self.duration_seconds)
                or self.duration_seconds < 0):
            raise EvalError("observation duration must be finite and non-negative")
        if isinstance(self.output_bytes, bool) or not isinstance(self.output_bytes, int) or self.output_bytes < 0:
            raise EvalError("observation output bytes must be a non-negative integer")
        if not isinstance(self.native_usage, Mapping):
            raise EvalError("observation native usage must be a mapping")
        usage: dict[str, float] = {}
        for meter, value in self.native_usage.items():
            if not isinstance(meter, str) or not meter.strip():
                raise EvalError("native usage meter must be a non-empty string")
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise EvalError("native usage values must be finite non-negative numbers")
            usage[meter] = value
        object.__setattr__(self, "native_usage", MappingProxyType(usage))


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def score(case: Case, observation: Observation) -> bool:
    """Return true only for a successful response containing expected text."""
    return observation.success and _normalized(case.expected_text) in _normalized(observation.text)


def aggregate(observations: Sequence[Observation]) -> dict[str, Any]:
    """Aggregate a homogeneous group without inventing a composite winner."""
    if not observations:
        return {
            "successRate": 0.0,
            "medianSeconds": 0.0,
            "rangeSeconds": [0.0, 0.0],
            "outputBytes": 0,
            "nativeUsage": {},
        }
    durations = sorted(item.duration_seconds for item in observations)
    usage: dict[str, float] = {}
    for item in observations:
        for meter, amount in item.native_usage.items():
            usage[meter] = usage.get(meter, 0) + amount
    return {
        "successRate": sum(item.success for item in observations) / len(observations),
        "medianSeconds": _median(durations),
        "rangeSeconds": [durations[0], durations[-1]],
        "outputBytes": sum(item.output_bytes for item in observations),
        "nativeUsage": usage,
    }


def _median(values: Sequence[float]) -> float:
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def load_cases(path: str | Path) -> list[Case]:
    """Load the fixed four public fixture classes from one checked-in JSON file."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"cannot read acquisition cases: {exc}") from exc
    if not isinstance(payload, list):
        raise EvalError("acquisition cases must be a JSON list")
    cases: list[Case] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise EvalError("acquisition case must be an object")
        cases.append(Case(
            raw.get("name"), raw.get("url"), raw.get("expectedText"), raw.get("kind"),
        ))
    _validate_case_set(cases)
    return cases


def _validate_case_set(cases: Sequence[Case]) -> None:
    if len(cases) != len(CASE_KINDS):
        raise EvalError("acquisition evaluation requires exactly four cases")
    names = [case.name for case in cases]
    if len(names) != len(set(names)):
        raise EvalError("acquisition case names must be unique")
    kinds = {case.kind for case in cases}
    if kinds != set(CASE_KINDS):
        raise EvalError("acquisition cases must cover html, javascript, text, and pdf once")


def case_set_hash(cases: Sequence[Case]) -> str:
    payload = [
        {"name": case.name, "url": case.url, "expectedText": case.expected_text, "kind": case.kind}
        for case in cases
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


async def crawltrove_observation(case: Case, client: httpx.AsyncClient) -> Observation:
    """Call the local API directly with its automatic acquisition route."""
    started = time.perf_counter()
    try:
        response = await client.post("/api/scrape", json={"url": case.url, "engine": "auto"})
    except httpx.HTTPError as exc:
        raise AdapterUnavailable("CrawlTrove request failed") from exc
    duration = time.perf_counter() - started
    if response.status_code != 200:
        raise AdapterUnavailable(f"CrawlTrove returned HTTP {response.status_code}")
    data = _json_object(response, "CrawlTrove")
    text = data.get("markdown")
    if not isinstance(text, str):
        raise AdapterUnavailable("CrawlTrove response did not include markdown")
    return Observation(True, text, duration, len(text.encode("utf-8")), {})


async def firecrawl_observation(case: Case, client: httpx.AsyncClient,
                                api_key: str) -> Observation:
    """Call Firecrawl's scrape endpoint with every cache path explicitly off."""
    started = time.perf_counter()
    try:
        response = await client.post(
            "/v2/scrape",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "url": case.url,
                "formats": ["markdown"],
                "maxAge": 0,
                "storeInCache": False,
            },
        )
    except httpx.HTTPError as exc:
        raise AdapterUnavailable("Firecrawl request failed") from exc
    duration = time.perf_counter() - started
    if response.status_code != 200:
        raise AdapterUnavailable(f"Firecrawl returned HTTP {response.status_code}")
    payload = _json_object(response, "Firecrawl")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    text = data.get("markdown")
    if payload.get("success") is False or not isinstance(text, str):
        raise AdapterUnavailable("Firecrawl response did not include markdown")
    return Observation(True, text, duration, len(text.encode("utf-8")), _firecrawl_usage(payload, data))


def _json_object(response: httpx.Response, adapter: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise AdapterUnavailable(f"{adapter} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AdapterUnavailable(f"{adapter} returned a non-object JSON response")
    return payload


def _firecrawl_usage(payload: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, float]:
    for source in (data, payload):
        for key in ("creditsUsed", "credits"):
            if key in source:
                value = source[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise AdapterUnavailable("Firecrawl returned nonnumeric native usage")
                return {"credits": value}
        usage = source.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise AdapterUnavailable("Firecrawl returned invalid native usage")
            return dict(usage)
    return {}


async def crawl4ai_observation(case: Case, *, timeout_seconds: float = 120.0) -> Observation:
    """Use a subprocess so Crawl4AI's cache and browser state cannot leak in."""
    payload = json.dumps({"url": case.url}, ensure_ascii=False).encode("utf-8")
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "eval.acquisition.crawl4ai_runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout=timeout_seconds)
    except (OSError, asyncio.TimeoutError) as exc:
        raise AdapterUnavailable("Crawl4AI subprocess unavailable") from exc
    if process.returncode != 0:
        raise AdapterUnavailable("Crawl4AI subprocess failed")
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdapterUnavailable("Crawl4AI subprocess returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("markdown"), str):
        raise AdapterUnavailable("Crawl4AI subprocess did not include markdown")
    return Observation(
        bool(result.get("success", True)), result["markdown"],
        float(result.get("durationSeconds", 0.0)),
        int(result.get("outputBytes", len(result["markdown"].encode("utf-8")))), {},
    )


async def preflight(cases: Sequence[Case], crawltrove_url: str,
                    firecrawl_api_key: str | None, tmp_dir: str | Path) -> None:
    """Validate every required local boundary before paid calls begin."""
    _validate_case_set(cases)
    if not firecrawl_api_key:
        raise EvalError("FIRECRAWL_API_KEY is required for acquisition evaluation")
    try:
        version = importlib.metadata.version("crawl4ai")
    except importlib.metadata.PackageNotFoundError as exc:
        raise EvalError("crawl4ai==0.9.2 is required; install requirements-eval.txt") from exc
    if version != "0.9.2":
        raise EvalError(f"crawl4ai==0.9.2 required, found {version}")
    for case in cases:
        try:
            await ensure_public_url(case.url)
        except UnsafeUrlError as exc:
            raise EvalError(f"{case.name}: case URL is not public") from exc
    tmp_path = Path(tmp_dir)
    tmp_path.mkdir(parents=True, exist_ok=True)
    if not os.access(tmp_path, os.W_OK):
        raise EvalError(f"evaluation output directory is not writable: {tmp_path}")
    async with httpx.AsyncClient(
        base_url=_base_url(crawltrove_url), timeout=15.0, trust_env=False,
    ) as client:
        try:
            response = await client.get("/api/health")
        except httpx.HTTPError as exc:
            raise EvalError("CrawlTrove health endpoint is unavailable") from exc
    if response.status_code != 200:
        raise EvalError(f"CrawlTrove health endpoint returned HTTP {response.status_code}")


def _base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise EvalError("CrawlTrove URL must be absolute HTTP(S)")
    return value.rstrip("/")


async def run_benchmark(cases: Sequence[Case], *, crawltrove_url: str,
                        firecrawl_api_key: str, tmp_dir: str | Path = "tmp",
                        runs: int = RUNS_PER_CASE, dry_run: bool = False,
                        crawltrove_api_key: str | None = None) -> dict[str, Any]:
    """Run one adapter check, then five fresh sequential calls per tool/case."""
    if runs != RUNS_PER_CASE:
        raise EvalError(f"acquisition evaluation requires exactly {RUNS_PER_CASE} runs per case")
    await preflight(cases, crawltrove_url, firecrawl_api_key, tmp_dir)
    local_headers = {"x-api-key": crawltrove_api_key} if crawltrove_api_key else {}
    async with httpx.AsyncClient(base_url=_base_url(crawltrove_url), timeout=120.0,
                                 headers=local_headers, trust_env=False) as local_client, \
            httpx.AsyncClient(base_url="https://api.firecrawl.dev", timeout=120.0,
                              trust_env=False) as firecrawl_client:
        adapters = {
            "crawltrove": lambda case: crawltrove_observation(case, local_client),
            "firecrawl": lambda case: firecrawl_observation(case, firecrawl_client, firecrawl_api_key),
            "crawl4ai": crawl4ai_observation,
        }
        simple = next(case for case in cases if case.kind == "html")
        for adapter in adapters.values():
            await adapter(simple)
        if dry_run:
            return {"dryRun": True, "case": simple.name}
        tool_reports: dict[str, Any] = {}
        for name, adapter in adapters.items():
            case_reports = []
            for case in cases:
                observations = []
                for _ in range(runs):
                    started = time.perf_counter()
                    try:
                        observation = await adapter(case)
                    except AdapterUnavailable:
                        observation = Observation(
                            False, "", time.perf_counter() - started, 0, {},
                        )
                    observations.append(observation)
                report = aggregate(observations)
                report.update({
                    "name": case.name,
                    "kind": case.kind,
                    "correctnessRate": sum(score(case, item) for item in observations) / runs,
                })
                case_reports.append(report)
            tool_reports[name] = {"cases": case_reports}
    return {
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "caseSetHash": case_set_hash(cases),
        "runsPerCase": runs,
        "tools": tool_reports,
    }


def redact_report(value: Any, *, secrets: Sequence[str] = ()) -> Any:
    """Drop response bodies and replace credentials before serializing a report."""
    if isinstance(value, Mapping):
        clean = {}
        for key, item in value.items():
            key_text = str(key)
            if _BODY_KEY.search(key_text):
                clean[key_text] = "[omitted]"
            elif _SENSITIVE_KEY.search(key_text):
                clean[key_text] = "[redacted]"
            else:
                clean[key_text] = redact_report(item, secrets=secrets)
        return clean
    if isinstance(value, list):
        return [redact_report(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        if any(secret and secret in value for secret in secrets):
            return "[redacted]"
        return value
    return value


def write_report(report: Mapping[str, Any], tmp_dir: str | Path = "tmp", *,
                 secrets: Sequence[str] = ()) -> Path:
    """Write one machine-readable, credential-free report under ignored tmp/."""
    safe = redact_report(report, secrets=secrets)
    target_dir = Path(tmp_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = target_dir / f"acquisition-eval-{stamp}.json"
    path.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
