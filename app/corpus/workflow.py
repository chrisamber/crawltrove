"""In-process fan-out runtime for the offline corpus drain.

The runtime provides named phases, a bounded-parallel fan-out (a barrier — it
awaits every task), and a progress reporter that shows running counters on a
TTY and flat, parseable log lines otherwise. Stdlib only and no corpus
knowledge — callers pass async task functions, so this module is unit-testable
in isolation.
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Sequence
from urllib.parse import urlsplit


@dataclass
class Result:
    """Outcome of one fan-out task. error is None on success."""

    item: Any
    value: Any = None
    error: Optional[str] = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


def host_of(url: str) -> str:
    """netloc of a URL, lowercased; '' if unparseable. Used for per-host caps."""
    try:
        return urlsplit(url).netloc.lower()
    except Exception:
        return ""


class Progress:
    """TTY-aware progress reporter.

    On a TTY each completion prints a running ``[done/total] STATUS label`` line;
    when piped/CI it emits one flushed ``  STATUS label`` line per transition so
    /loop and logs can parse it. Pass a ``stream`` to capture output in tests.
    """

    def __init__(self, stream=None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._total = 0
        self._done = 0

    def phase(self, title: str, total: int = 0) -> None:
        self._total, self._done = total, 0
        self._line(f"phase: {title}" + (f" ({total} tasks)" if total else ""))

    def task_start(self, label: str) -> None:
        if not self.tty:  # on a TTY we report only on completion (less noise)
            self._line(f"  start {label}")

    def task_done(self, label: str, *, ok: bool, detail: str = "") -> None:
        self._done += 1
        status = "ok" if ok else "FAIL"
        prefix = f"  [{self._done}/{self._total}] " if self.tty else "  "
        self._line(f"{prefix}{status} {label}" + (f" — {detail}" if detail else ""))

    def _line(self, text: str) -> None:
        print(text, file=self.stream, flush=True)


async def fan_out(
    items: Sequence[Any],
    task_fn: Callable[[Any], Awaitable[Any]],
    *,
    concurrency: int = 3,
    per_host: Optional[int] = None,
    host_fn: Optional[Callable[[Any], str]] = None,
    label_fn: Optional[Callable[[Any], str]] = None,
    progress: Optional[Progress] = None,
) -> List[Result]:
    """Run task_fn over items, at most `concurrency` in flight (a barrier: awaits
    all). A task that raises becomes a failed Result and never aborts siblings.
    With per_host set, at most `per_host` tasks share a host at once. Results are
    returned in the order of `items`.
    """
    sem = asyncio.Semaphore(concurrency)
    host_sems: dict = {}
    host_lock = asyncio.Lock()

    async def _host_sem(item):
        if not per_host or per_host < 1:  # None / 0 / negative -> no per-host cap (0 == unlimited; never build Semaphore(0))
            return None
        host = host_fn(item) if host_fn else ""
        if not host:
            return None
        async with host_lock:
            host_sems.setdefault(host, asyncio.Semaphore(per_host))
            return host_sems[host]

    async def _do(item, label) -> Result:
        if progress is not None:
            progress.task_start(label)
        start = time.monotonic()
        try:
            value = await task_fn(item)
            res = Result(item=item, value=value, elapsed=time.monotonic() - start)
        except Exception as exc:  # one bad task never kills the fan-out
            res = Result(item=item, error=f"{type(exc).__name__}: {exc}",
                         elapsed=time.monotonic() - start)
        if progress is not None:
            progress.task_done(label, ok=res.ok, detail=res.error or "")
        return res

    async def _run(item) -> Result:
        label = label_fn(item) if label_fn else str(item)
        hsem = await _host_sem(item)
        async with sem:
            if hsem is None:
                return await _do(item, label)
            async with hsem:
                return await _do(item, label)

    return list(await asyncio.gather(*(_run(it) for it in items)))
