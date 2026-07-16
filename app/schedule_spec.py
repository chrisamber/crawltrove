"""Minimal, dependency-free schedule spec parsing for the in-process scheduler.

We deliberately avoid cron/APScheduler. A job's `schedule` is a short TEXT spec
interpreted as a fixed interval:

    None / "" / "manual"   -> no automatic runs (manual /run only)
    "3600"                 -> every 3600 seconds
    "30s" / "15m" / "2h" / "1d"   -> unit-suffixed interval
    "every 15m"            -> same, with a leading "every"
    "@minutely/@hourly/@daily/@weekly" -> 60 / 3600 / 86400 / 604800

Anything unparseable returns None (treated as "manual only") and the caller logs
a warning. Intervals are clamped to a 5-second floor to avoid hot loops.
"""
import datetime
import re
from typing import Optional

_MIN_INTERVAL_S = 5
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_ALIASES = {"@minutely": 60, "@hourly": 3600, "@daily": 86400, "@weekly": 604800}


def parse_interval_seconds(schedule: Optional[str]) -> Optional[int]:
    """Return the interval in seconds, or None for 'no automatic runs'."""
    if not schedule:
        return None
    s = schedule.strip().lower()
    if s in ("", "manual", "none", "off"):
        return None
    if s in _ALIASES:
        return _ALIASES[s]
    if s.startswith("every "):
        s = s[6:].strip()
    if s.isdigit():
        return max(_MIN_INTERVAL_S, int(s))
    m = re.fullmatch(r"(\d+)\s*([smhdw])", s)
    if m:
        return max(_MIN_INTERVAL_S, int(m.group(1)) * _UNIT_SECONDS[m.group(2)])
    return None


def next_run_at(
    schedule: Optional[str],
    from_time: Optional[datetime.datetime] = None,
) -> Optional[datetime.datetime]:
    """Compute the next fire time (tz-aware UTC), or None if not scheduled."""
    interval = parse_interval_seconds(schedule)
    if interval is None:
        return None
    base = from_time or datetime.datetime.now(datetime.timezone.utc)
    return base + datetime.timedelta(seconds=interval)
