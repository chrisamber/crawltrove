import asyncio
import io

from app.corpus import workflow as wf


async def _peak_concurrency(items, **kw):
    live = 0
    peak = 0

    async def task(x):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return x

    results = await wf.fan_out(items, task, **kw)
    return results, peak


def test_fan_out_collects_all_results_in_order():
    async def task(x):
        await asyncio.sleep(0)
        return x * 2

    results = asyncio.run(wf.fan_out([1, 2, 3], task, concurrency=2))
    assert [r.value for r in results] == [2, 4, 6]
    assert all(r.ok for r in results)


def test_fan_out_bounds_concurrency():
    results, peak = asyncio.run(_peak_concurrency(list(range(6)), concurrency=2))
    assert peak <= 2
    assert len(results) == 6


def test_fan_out_isolates_errors():
    async def task(x):
        await asyncio.sleep(0)
        if x == 2:
            raise ValueError("boom")
        return x

    results = asyncio.run(wf.fan_out([1, 2, 3], task, concurrency=3))
    by_item = {r.item: r for r in results}
    assert by_item[2].ok is False
    assert "boom" in by_item[2].error
    assert by_item[1].ok and by_item[3].ok


def test_fan_out_per_host_cap():
    peaks = {}
    live = {}

    async def task(url):
        h = wf.host_of(url)
        live[h] = live.get(h, 0) + 1
        peaks[h] = max(peaks.get(h, 0), live[h])
        await asyncio.sleep(0.02)
        live[h] -= 1
        return url

    items = ["https://a.com/1", "https://a.com/2", "https://b.com/1", "https://b.com/2"]
    asyncio.run(wf.fan_out(items, task, concurrency=10, per_host=1, host_fn=wf.host_of))
    assert peaks["a.com"] == 1
    assert peaks["b.com"] == 1


def test_progress_non_tty_emits_parseable_lines():
    buf = io.StringIO()  # StringIO.isatty() -> False
    p = wf.Progress(stream=buf)
    assert p.tty is False
    p.phase("Scrape", total=2)
    p.task_start("batch-a")
    p.task_done("batch-a", ok=True)
    p.task_done("batch-b", ok=False, detail="HTTP 500")
    out = buf.getvalue()
    assert "phase: Scrape" in out
    assert "ok batch-a" in out
    assert "FAIL batch-b" in out
    assert "HTTP 500" in out


def test_fan_out_per_host_zero_means_unlimited():
    peak = 0
    live = 0

    async def task(url):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return url

    items = ["https://a.com/1", "https://a.com/2", "https://a.com/3"]
    asyncio.run(wf.fan_out(items, task, concurrency=10, per_host=0, host_fn=wf.host_of))
    assert peak >= 2  # per_host=0 disables the per-host cap; same-host tasks overlap
