import asyncio
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from bs4 import BeautifulSoup
from markdownify import markdownify

from app import documents, extraction, fetch, lang, license_detect, quality
from app import actions as page_actions
from app.documents import vision
from app.url_safety import UnsafeUrlError, ensure_public_url

logger = logging.getLogger("scraper")


async def _guard_browser_request(route) -> bool:
    """Block browser navigation and subresources that target private networks."""
    try:
        await ensure_public_url(route.request.url)
    except UnsafeUrlError:
        await route.abort("blockedbyclient")
        return False
    await route.continue_()
    return True


async def _guard_browser_websocket(websocket_route) -> bool:
    """Apply the same target policy to WebSockets created by a page."""
    url = websocket_route.url
    if url.startswith("wss://"):
        url = "https://" + url[6:]
    elif url.startswith("ws://"):
        url = "http://" + url[5:]
    try:
        await ensure_public_url(url)
    except UnsafeUrlError:
        await websocket_route.close(code=1008, reason="Blocked network target")
        return False
    websocket_route.connect_to_server()
    return True


def _launch_kwargs() -> Dict[str, Any]:
    """Chromium launch arguments for the Playwright tier.

    CHROMIUM_EXECUTABLE_PATH points the launch at a specific browser binary for
    environments where the Playwright-pinned revision isn't installed (e.g. a
    preinstalled system chromium). Unset — the default everywhere Playwright's
    own browser download ran, including the Docker image — the kwarg is omitted
    and launch behavior is unchanged.
    """
    kwargs: Dict[str, Any] = {
        "headless": True,
        "chromium_sandbox": True,
        "args": [
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    }
    if os.environ.get("CHROMIUM_DISABLE_SANDBOX", "").lower() in (
        "1", "true", "yes"
    ):
        kwargs["chromium_sandbox"] = False
        kwargs["args"].extend(["--no-sandbox", "--disable-setuid-sandbox"])
    exe = os.environ.get("CHROMIUM_EXECUTABLE_PATH")
    if exe:
        kwargs["executable_path"] = exe
    return kwargs


def _context_kwargs() -> Dict[str, Any]:
    """Browser-context defaults that keep all page traffic observable."""
    return {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1280, "height": 800},
        "ignore_https_errors": True,
        # Service-worker fetches bypass Playwright route handlers, so disable
        # them to keep the private-network request guard complete.
        "service_workers": "block",
    }


def _run_signal(name: str, fn: Callable[[], Any], default: Any,
                errors: List[Dict[str, str]], url: str) -> Any:
    """Run one corpus-signal call, capturing a failure instead of propagating it.

    The signal modules already swallow internally (one bad signal never fails a
    scrape); this outer guard adds a WARNING log + a structured record in
    metadata.signal_errors so the persistence layer can mirror it to
    scrape_errors(stage='signal:<name>'). Resilience is preserved either way.
    """
    try:
        return fn()
    except Exception as e:  # pragma: no cover - signals rarely raise
        logger.warning("signal %s failed for %s: %s", name, url, e)
        errors.append({"signal": name, "message": str(e)[:500]})
        return default

class WebScraper:
    def __init__(self):
        pass

    async def scrape(self, url: str, wait_for_ms: int = 1000, only_main_content: bool = True,
                     engine: str = "auto",
                     actions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Scrape a URL and convert to Markdown.

        engine: "auto" tries a cheap impersonated HTTP fetch first and only
        renders with Playwright when the response looks like a JS shell or a
        bot challenge; "http" / "browser" force a single tier. A non-empty
        `actions` list (wait/click/scroll/fill/press) forces the browser tier
        and runs between navigation and content capture.
        """
        engine = page_actions.effective_engine(engine, actions)
        # Tier 1: impersonated HTTP fetch, no browser
        if engine in ("auto", "http"):
            resp = await fetch.fetch_http(url)
            if resp is None:
                return self._error_result(
                    url, "HTTP fetch failed (transport error)",
                    reason="transport_error", engine_used="http",
                )
            status_code = resp.get("status")
            if not isinstance(status_code, int) or not 200 <= status_code < 300:
                return self._error_result(
                    url, f"HTTP fetch failed (status {status_code})",
                    reason="http_status_error", status_code=status_code,
                    engine_used="http",
                )
            # Documents (PDF/EPUB) are handled here — the browser tier can't
            # render them anyway (chromium downloads binaries).
            kind = documents.sniff(resp.get("content_type", ""), url)
            if kind:
                doc_result = await self._build_document_result(resp, url, kind)
                if doc_result:
                    return doc_result
                # Identified as a document but extraction failed (e.g. a scanned
                # PDF with no OCR available, or a corrupt EPUB). Do NOT fall
                # through to Playwright: chromium downloads documents instead of
                # rendering them ("Page.goto: Download is starting"), surfacing
                # as a 502. Degrade on the raw bytes here so the scrape still
                # succeeds (spec §11.5).
                return self._build_result(resp["html"], url, only_main_content,
                                          engine_used="http", status_code=resp.get("status"))
            if engine == "http":
                return self._build_result(resp["html"], url, only_main_content,
                                          engine_used="http", status_code=resp.get("status"))
            if not fetch.needs_browser(resp):
                return self._build_result(resp["html"], url, only_main_content,
                                          engine_used="http", status_code=resp.get("status"))

        # Tier 2: full Playwright render
        await ensure_public_url(url)
        async with async_playwright() as p:
            browser = None
            blocked_navigations = []
            try:
                browser = await p.chromium.launch(**_launch_kwargs())

                # Setup context with mobile-friendly user agent to prevent bot blocking
                context = await browser.new_context(**_context_kwargs())

                async def guard_request(route):
                    allowed = await _guard_browser_request(route)
                    if not allowed and route.request.is_navigation_request():
                        blocked_navigations.append(route.request.url)

                await context.route("**/*", guard_request)
                await context.route_web_socket("**/*", _guard_browser_websocket)
                page = await context.new_page()

                # Mask headless-Chromium fingerprints (webdriver flag, plugins,
                # chrome.runtime, …) before any site script runs
                try:
                    await stealth_async(page)
                except Exception:
                    pass

                # Go to URL with reasonable timeout
                nav_response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                status_code = nav_response.status if nav_response else None

                # Let dynamic scripts complete loading if requested
                if wait_for_ms > 0:
                    await asyncio.sleep(wait_for_ms / 1000.0)

                # Pre-capture page actions (never abort the scrape; outcomes
                # are reported in metadata.actions).
                action_outcomes = None
                if actions:
                    action_outcomes = await page_actions.run_actions(page, actions)

                # Fetch content
                html_content = await page.content()
                if fetch.is_challenge_html(html_content):
                    return self._error_result(
                        url, "Browser render returned a challenge page",
                        reason="blocked_challenge", status_code=status_code,
                        engine_used="browser",
                    )
                if not isinstance(status_code, int) or not 200 <= status_code < 300:
                    return self._error_result(
                        url, f"Browser navigation failed (status {status_code})",
                        reason="http_status_error", status_code=status_code,
                        engine_used="browser",
                    )
                title = await page.title()

                # Optional full-page screenshot for raw capture (best-effort —
                # a screenshot failure must never fail the scrape).
                screenshot = None
                try:
                    screenshot = await page.screenshot(full_page=True)
                except Exception:
                    screenshot = None

                # Query synchronously in the current DOM; locators wait for a
                # missing element and add seconds to pages without descriptions.
                try:
                    description = await page.evaluate("""() => {
                        const meta = document.querySelector("meta[name='description']")
                            || document.querySelector("meta[property='og:description']");
                        return meta ? meta.getAttribute("content") || "" : "";
                    }""")
                except Exception:
                    description = ""

                result = self._build_result(
                    html_content, url, only_main_content,
                    engine_used="browser", title=title, description=description.strip(),
                    status_code=status_code,
                )
                if screenshot:
                    result["_raw"]["screenshot"] = screenshot
                if action_outcomes is not None:
                    result["metadata"]["actions"] = action_outcomes
                return result

            except UnsafeUrlError:
                raise
            except Exception as e:
                if blocked_navigations:
                    raise UnsafeUrlError(
                        "Refusing browser navigation to a non-public network address"
                    ) from e
                return self._error_result(url, str(e))
            finally:
                if browser:
                    await browser.close()

    async def _build_document_result(self, resp: Dict[str, Any], url: str,
                                     kind: str) -> Optional[Dict[str, Any]]:
        """Assemble a result for a fetched document (PDF/EPUB/image); None if extraction fails."""
        parsed = documents.parse(kind, resp.get("content") or b"")
        if not parsed:
            return None
        # Vision-LLM OCR escalation (off by default; app/documents/vision.py).
        # Runs BEFORE license/quality/language so the signals see the final
        # markdown. escalate() swallows all failures and returns the tesseract
        # version — it can never fail the scrape.
        parsed = await vision.escalate(parsed, resp.get("content") or b"", kind)
        markdown = parsed["markdown"]
        ocr_info = parsed.get("ocr")
        metadata = {
            "title": parsed["title"],
            "description": "",
            "url": url,
            "engine": "http",
            "extractor": parsed["extractor"],
            "status_code": resp.get("status"),
            "pages": parsed["pages"],
            # license/quality/language run on the extracted markdown for free
            "license": license_detect.detect_text(markdown),
            "quality": quality.assess(markdown),
            "language": lang.detect(markdown),
        }
        if ocr_info:
            metadata["ocr"] = ocr_info
        return {
            "success": True,
            "url": url,
            "title": parsed["title"],
            "description": "",
            "markdown": markdown,
            "html": "",
            "metadata": metadata,
            "_raw": {"html": ""},
        }

    def _build_result(self, html_content: str, url: str, only_main_content: bool,
                      engine_used: str, title: str = "", description: str = "",
                      status_code: Optional[int] = None) -> Dict[str, Any]:
        """Run the extraction pipeline on raw HTML and assemble the API result.

        status_code (the HTTP status of the fetch) and the verbatim pre-clean
        html are threaded out additively for raw capture: status_code
        lands in metadata; the raw html rides on a private `_raw` channel the
        persistence layer consumes and strips before the API response is saved.
        """
        signal_errors: List[Dict[str, str]] = []

        # License markers live in footers — detect on the RAW html before cleaning
        license_info = _run_signal(
            "license", lambda: license_detect.detect(html_content), None, signal_errors, url)

        cleaned_html, markdown = self.clean_and_convert(html_content, url, only_main_content)
        extractor = "legacy"

        # Trafilatura main-content extraction (FineWeb/RefinedWeb's extractor);
        # fall back to the legacy cleaner output when it returns nothing usable
        if only_main_content:
            extracted = _run_signal(
                "extraction", lambda: extraction.extract(html_content, url), None, signal_errors, url)
            if extracted:
                markdown = extracted["markdown"]
                extractor = "trafilatura"
                meta = extracted["meta"]
                title = title or meta.get("title", "")
                description = description or meta.get("description", "")
                if not license_info and meta.get("license"):
                    license_info = {"id": meta["license"], "url": "", "source": "trafilatura", "evidence": ""}

        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", html_content, re.S | re.I)
            title = m.group(1).strip() if m else ""

        quality_report = _run_signal(
            "quality", lambda: quality.assess(markdown), None, signal_errors, url)
        language = _run_signal(
            "language", lambda: lang.detect(markdown), None, signal_errors, url)

        metadata = {
            "title": title,
            "description": description,
            "url": url,
            "engine": engine_used,
            "extractor": extractor,
            "status_code": status_code,
            "license": license_info,
            "quality": quality_report,
            "language": language,
        }
        if signal_errors:
            metadata["signal_errors"] = signal_errors

        return {
            "success": True,
            "url": url,
            "title": title,
            "description": description,
            "markdown": markdown,
            "html": cleaned_html,
            "metadata": metadata,
            # Private raw-capture channel (stripped before the API response /
            # saved JSON; persistence writes it to data/runs/<stem>/page-N.html.txt).
            "_raw": {"html": html_content},
        }

    def _error_result(self, url: str, error: str, reason: Optional[str] = None,
                      status_code: Optional[int] = None,
                      engine_used: Optional[str] = None) -> Dict[str, Any]:
        metadata = {
            "title": "", "description": "", "url": url,
            "reason": reason, "status_code": status_code,
            "engine": engine_used,
        }
        return {
            "success": False,
            "url": url,
            "error": error,
            "markdown": "",
            "html": "",
            "metadata": metadata,
        }

    def clean_and_convert(self, html_content: str, base_url: str, only_main_content: bool) -> Tuple[str, str]:
        """Cleans boilerplates and returns (cleaned_html, markdown_content)."""
        soup = BeautifulSoup(html_content, "html.parser")

        # Resolve all relative links/images to absolute paths
        for a in soup.find_all("a", href=True):
            a["href"] = urljoin(base_url, a["href"])
        for img in soup.find_all("img", src=True):
            img["src"] = urljoin(base_url, img["src"])

        # Strip out interactive and style elements
        for element in soup(["script", "style", "iframe", "noscript", "svg", "form", "button", "input", "select", "style", "dialog"]):
            element.decompose()

        target = soup
        if only_main_content:
            # Common main content selectors
            main_selectors = ["main", "article", "[role='main']", "#content", ".content", "#main", ".main"]
            found = False
            for selector in main_selectors:
                el = soup.select_one(selector)
                if el:
                    # Ensure container has text to make sure it's not a dummy container
                    if len(el.get_text(strip=True)) > 200:
                        target = el
                        found = True
                        break

            # If no main container found, strip navigation/footers
            if not found:
                for element in soup(["header", "footer", "nav", ".header", ".footer", ".nav", "#header", "#footer", "#nav", "aside", ".aside"]):
                    element.decompose()

        cleaned_html = str(target)

        # Convert to clean Markdown
        markdown = markdownify(cleaned_html, heading_style="ATX", bullets="-")

        # Standardize whitespace and extra newlines
        markdown = re.sub(r'\n{3,}', '\n\n', markdown)
        markdown = markdown.strip()

        return cleaned_html, markdown
