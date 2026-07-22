"""Pure robots and retry policy shared by durable crawl workers."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Literal, Optional
from urllib.parse import quote, unquote, urlparse, urlunparse
from urllib.robotparser import RobotFileParser


@dataclass(frozen=True)
class RobotsOutcome:
    allowed: bool
    state: str
    code: Optional[str]


@dataclass(frozen=True)
class RobotsResponseOutcome:
    action: Literal["parse", "refresh", "allow", "deny", "defer", "retry"]
    retry_at: Optional[datetime] = None


def _robots_path(url: str) -> str:
    parsed = urlparse(unquote(url))
    path = urlunparse(("", "", parsed.path, parsed.params, parsed.query, ""))
    return quote(path) or "/"


def robots_decision(body: str, user_agent: str, url: str) -> bool:
    """Evaluate a robots body for one URL using the most specific rule.

    ``RobotFileParser`` owns directive grouping and product matching. Python's
    implementation otherwise uses the first matching path rule, so this small
    final selection applies the robots standard's longest-match rule and lets
    Allow win an equal-length tie.
    """
    parser = RobotFileParser()
    parser.parse((body or "").splitlines())

    entry = next(
        (candidate for candidate in parser.entries
         if candidate.applies_to(user_agent)),
        None,
    )
    if entry is None:
        entry = parser.default_entry
    if entry is None:
        return True

    path = _robots_path(url)
    matches = [rule for rule in entry.rulelines if rule.applies_to(path)]
    if not matches:
        return True
    longest = max(len(rule.path) for rule in matches)
    most_specific = [rule for rule in matches if len(rule.path) == longest]
    return any(rule.allowance for rule in most_specific)


def robots_outcome(*, allowed: bool, is_seed: bool) -> RobotsOutcome:
    """Map a robots decision to stable durable task semantics."""
    if allowed:
        return RobotsOutcome(True, "allowed", None)
    if is_seed:
        return RobotsOutcome(False, "permanent_failed", "seed_blocked_by_robots")
    return RobotsOutcome(False, "blocked_robots", "blocked_robots")


def parse_retry_after(value: str, now: datetime) -> Optional[datetime]:
    """Parse Retry-After seconds or an HTTP date without shortening it."""
    raw = (value or "").strip()
    if not raw:
        return None

    reference = now
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    if raw.isdigit():
        try:
            return reference + timedelta(seconds=int(raw))
        except OverflowError:
            return None

    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    reference = reference.astimezone(timezone.utc)
    return max(parsed, reference)


def classify_robots_response(
    status_code: Optional[int],
    *,
    now: datetime,
    retry_after: Optional[str] = None,
) -> RobotsResponseOutcome:
    """Classify a robots fetch without performing network or state changes."""
    if status_code is not None and 200 <= status_code <= 299:
        return RobotsResponseOutcome("parse")
    if status_code == 304:
        return RobotsResponseOutcome("refresh")
    if status_code in (401, 403):
        return RobotsResponseOutcome("deny")
    if status_code in (404, 410):
        return RobotsResponseOutcome("allow")
    if status_code == 429:
        return RobotsResponseOutcome(
            "defer", parse_retry_after(retry_after or "", now)
        )
    # Transport failures, 5xx, and unrecognized policy responses remain
    # unresolved. The durable scheduler decides their bounded retry timing.
    return RobotsResponseOutcome("retry")


__all__ = [
    "RobotsOutcome",
    "RobotsResponseOutcome",
    "classify_robots_response",
    "parse_retry_after",
    "robots_decision",
    "robots_outcome",
]
