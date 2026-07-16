"""Pre-scrape page actions for the browser tier.

An ordered list of interactions executed between navigation and content
capture: wait / click / scroll / fill / press. Actions imply browser rendering
(`effective_engine`), and each action's outcome is recorded in
metadata.actions — a failing action never aborts the remaining actions or the
scrape (resilient convention); capture proceeds with whatever DOM state
resulted.

`run_actions` takes any object with the Playwright Page surface it uses
(wait_for_timeout / wait_for_selector / click / fill / evaluate / keyboard),
so tests drive it with a fake page — the tier-2 browser is never required.
"""
import logging
from typing import Any, Dict, List

logger = logging.getLogger("actions")

ACTION_TYPES = {"wait", "click", "scroll", "fill", "press"}
MAX_ACTIONS = 20
MAX_WAIT_MS = 10_000
SELECTOR_TIMEOUT_MS = 5_000


def effective_engine(engine: str, actions: Any) -> str:
    """Actions imply browser rendering: the HTTP tier
    has no DOM to act on, so any non-empty actions list forces tier 2."""
    return "browser" if actions else engine


async def _run_one(page: Any, action: Dict[str, Any]) -> None:
    kind = action.get("type")
    if kind == "wait":
        selector = action.get("selector")
        if selector:
            await page.wait_for_selector(selector, timeout=SELECTOR_TIMEOUT_MS)
        else:
            ms = min(int(action.get("milliseconds") or 0), MAX_WAIT_MS)
            await page.wait_for_timeout(ms)
    elif kind == "click":
        await page.click(action["selector"], timeout=SELECTOR_TIMEOUT_MS)
    elif kind == "scroll":
        direction = action.get("direction") or "down"
        sign = "-" if direction == "up" else ""
        await page.evaluate(f"window.scrollBy(0, {sign}0.9 * window.innerHeight)")
    elif kind == "fill":
        await page.fill(action["selector"], action.get("text") or "",
                        timeout=SELECTOR_TIMEOUT_MS)
    elif kind == "press":
        await page.keyboard.press(action["key"])
    else:
        raise ValueError(f"unknown action type: {kind!r}")


async def run_actions(page: Any, actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Execute actions in order against a live page; one outcome per action.

    Never raises: a failing action is recorded ({ok: False, error}) and the
    rest still run, so a broken selector degrades the interaction, not the
    scrape.
    """
    outcomes: List[Dict[str, Any]] = []
    for action in actions[:MAX_ACTIONS]:
        outcome = {"type": action.get("type"), "ok": True, "error": None}
        try:
            await _run_one(page, action)
        except Exception as e:
            outcome["ok"] = False
            outcome["error"] = str(e)[:300]
            logger.warning("action %s failed: %s", action.get("type"), e)
        outcomes.append(outcome)
    return outcomes
