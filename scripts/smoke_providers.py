"""Private, one-call release smoke for managed acquisition providers."""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_URL = "https://example.com"
DEFAULT_EXPECTED_TEXT = "Example Domain"
REQUIRED_PROVIDER_ENV = (
    "FIRECRAWL_API_KEY",
    "BRIGHTDATA_API_KEY",
    "BRIGHTDATA_UNLOCKER_ZONE",
    "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID",
)
REQUIRED_S3_ENV = (
    "S3_BUCKET",
    "S3_ENDPOINT_URL",
    "S3_REGION",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
)


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--expected-text", default=DEFAULT_EXPECTED_TEXT)
    parser.add_argument("--firecrawl-credits", type=_positive, default=1)
    parser.add_argument("--brightdata-requests", type=_positive, default=1)
    parser.add_argument("--browserbase-minutes", type=_positive, default=1)
    parser.add_argument("--allow-higher-budget", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _missing(names: Sequence[str]) -> list[str]:
    return [name for name in names if not os.environ.get(name, "").strip()]


def _budget_errors(args: argparse.Namespace) -> list[str]:
    if args.allow_higher_budget:
        return []
    limits = {
        "firecrawl credits": args.firecrawl_credits,
        "Bright Data requests": args.brightdata_requests,
        "Browserbase minutes": args.browserbase_minutes,
    }
    return [f"{name} exceeds the one-unit release cap" for name, value in limits.items() if value > 1]


async def _database_ready() -> bool:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        return False
    try:
        import asyncpg

        connection = await asyncpg.connect(dsn, timeout=10)
        try:
            await connection.execute("SELECT 1")
        finally:
            await connection.close()
    except Exception:
        return False
    return True


def _s3_ready() -> bool:
    try:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            region_name=os.environ["S3_REGION"],
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        )
        client.head_bucket(Bucket=os.environ["S3_BUCKET"])
    except Exception:
        return False
    return True


async def preflight(args: argparse.Namespace) -> list[str]:
    """Return redacted configuration/readiness failures before any paid call."""
    errors = _budget_errors(args)
    missing = _missing(REQUIRED_PROVIDER_ENV)
    if missing:
        errors.extend(f"missing {name}" for name in missing)
        return errors
    if os.environ.get("ARTIFACT_STORE_BACKEND", "").lower() != "s3":
        errors.append("ARTIFACT_STORE_BACKEND must be s3")
    missing_s3 = _missing(REQUIRED_S3_ENV)
    errors.extend(f"missing {name}" for name in missing_s3)
    if not os.environ.get("DATABASE_URL", "").strip():
        errors.append("missing DATABASE_URL")
    if errors:
        return errors

    from app.url_safety import UnsafeUrlError, ensure_public_url

    try:
        await ensure_public_url(args.url)
    except (UnsafeUrlError, ValueError):
        errors.append("smoke URL is not a public HTTP(S) target")
    if not await _database_ready():
        errors.append("database is not ready")
    if not _s3_ready():
        errors.append("S3 storage is not ready")
    return errors


def _provider_rows(args: argparse.Namespace) -> list[tuple[str, Any, Any]]:
    """Import providers only after preflight so missing integrations stay harmless."""
    from app.acquisition.brightdata import BrightDataAdapter
    from app.acquisition.browserbase import BrowserbaseAdapter
    from app.acquisition.firecrawl import FirecrawlAdapter
    from app.acquisition.providers import ProviderRequest

    timeout_seconds = max(60, math.ceil(args.browserbase_minutes * 60))
    return [
        (
            "firecrawl",
            FirecrawlAdapter(
                os.environ["FIRECRAWL_API_KEY"],
                base_url=os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev"),
            ),
            ProviderRequest(args.url, "firecrawl_scrape", 60, True),
        ),
        (
            "brightdata",
            BrightDataAdapter(
                os.environ["BRIGHTDATA_API_KEY"], os.environ["BRIGHTDATA_UNLOCKER_ZONE"],
                api_url=os.environ.get("BRIGHTDATA_API_URL", "https://api.brightdata.com/request"),
            ),
            ProviderRequest(args.url, "brightdata_unlocker", 60, True),
        ),
        (
            "browserbase",
            BrowserbaseAdapter(
                os.environ["BROWSERBASE_API_KEY"], os.environ["BROWSERBASE_PROJECT_ID"],
            ),
            ProviderRequest(args.url, "browserbase_session", timeout_seconds, True),
        ),
    ]


def _within_budget(name: str, usage: Mapping[str, int | float], args: argparse.Namespace) -> bool:
    if name == "firecrawl":
        return set(usage) == {"credits"} and usage["credits"] <= args.firecrawl_credits
    if name == "brightdata":
        return set(usage) == {"requests"} and usage["requests"] <= args.brightdata_requests
    return (
        set(usage) == {"browserMinutes", "proxyBytes"}
        and usage["browserMinutes"] <= args.browserbase_minutes
        and usage["proxyBytes"] == 0
    )


async def run_smoke(args: argparse.Namespace) -> int:
    errors = await preflight(args)
    if errors:
        print("preflight failed: " + "; ".join(errors), file=sys.stderr)
        return 2
    if args.preflight:
        print("preflight ok")
        return 0
    if args.dry_run:
        print("dry-run ok")
        return 0

    from app.scraper import WebScraper

    scraper = WebScraper()
    try:
        try:
            rows = _provider_rows(args)
        except Exception:
            print("provider configuration failed", file=sys.stderr)
            return 1
        for name, adapter, request in rows:
            if not adapter.available():
                print(f"{name} failed", file=sys.stderr)
                return 1
            started = time.monotonic()
            try:
                result = await adapter.acquire(request)
                extracted = scraper._build_result(
                    result.raw_html, result.final_url, request.only_main_content,
                    engine_used=name, status_code=result.status_code,
                )
                text = " ".join((str(extracted.get("title", "")), str(extracted.get("markdown", ""))))
                if args.expected_text.casefold() not in text.casefold():
                    print(f"{name} failed", file=sys.stderr)
                    return 1
                usage = result.native_cost.values
                if not _within_budget(name, usage, args):
                    print(f"{name} failed", file=sys.stderr)
                    return 1
            except Exception:
                print(f"{name} failed", file=sys.stderr)
                return 1
            finally:
                close = getattr(adapter, "aclose", None)
                if close is not None:
                    await close()
            elapsed = time.monotonic() - started
            print(f"{name} ok {elapsed:.1f}s usage={dict(usage)}")
    finally:
        await scraper.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run_smoke(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
