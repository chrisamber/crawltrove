import asyncio
import logging
import os
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from bs4 import BeautifulSoup
from markdownify import markdownify

from app import documents, extraction, fetch, lang, license_detect, quality
from app import actions as page_actions
from app.acquisition.captcha import CaptchaPolicy, solve_if_authorized
from app.documents import vision
from app.url_safety import UnsafeUrlError, ensure_public_url

logger = logging.getLogger("scraper")

MAX_DOM_BYTES = 10 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024


def _captcha_metadata(result) -> dict[str, str] | None:
    """Expose only classified CAPTCHA outcome, never an answer or page payload."""
    if result.kind is None:
        return None
    return {"kind": result.kind, "outcome": result.state}


def _attach_captcha_metadata(result: Dict[str, Any], captcha: dict[str, str] | None) -> Dict[str, Any]:
    if captcha and isinstance(result.get("metadata"), dict):
        result["metadata"]["captcha"] = captcha
    return result


async def _guard_browser_websocket(websocket_route) -> bool:
    """Block WebSockets: Chromium cannot apply the pinned HTTP transport to them."""
    await websocket_route.close(code=1008, reason="WebSockets disabled")
    return False


def _same_origin(first: str, second: str) -> bool:
    return (urlsplit(first).scheme, urlsplit(first).netloc) == (
        urlsplit(second).scheme, urlsplit(second).netloc
    )


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
        "ignore_https_errors": False,
        # Service-worker fetches bypass Playwright route handlers, so disable
        # them to keep the private-network request guard complete.
        "service_workers": "block",
    }


class BrowserRuntime:
    """One Chromium process with isolated contexts for individual renders."""

    def __init__(self, playwright_factory=None, max_contexts: int = 2,
                 transport_factory=fetch.HttpFetcher):
        self._playwright_factory = playwright_factory or async_playwright
        self._playwright_manager = None
        self._playwright = None
        self._browser = None
        self._contexts = asyncio.Semaphore(max_contexts)
        self._completed_contexts = 0
        self._restart_after = int(os.environ.get("BROWSER_RESTART_CONTEXTS", "100"))
        self._transport_factory = transport_factory

    async def start(self) -> None:
        if self._browser is not None:
            return
        manager = self._playwright_factory()
        self._playwright_manager = manager
        if hasattr(manager, "start"):
            self._playwright = await manager.start()
        else:  # Compatibility with lightweight Playwright test doubles.
            self._playwright = await manager.__aenter__()
        self._browser = await self._playwright.chromium.launch(**_launch_kwargs())

    async def close(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            await browser.close()
        manager, self._playwright_manager = self._playwright_manager, None
        self._playwright = None
        if manager is not None:
            if hasattr(manager, "stop"):
                await manager.stop()
            elif hasattr(manager, "__aexit__"):
                await manager.__aexit__(None, None, None)

    def _new_transport(self, proxy: Optional[Dict[str, str]]):
        if proxy is None:
            return self._transport_factory()
        return self._transport_factory(
            proxy=proxy["server"],
            proxy_auth=(proxy["username"], proxy["password"])
            if "username" in proxy else None,
        )

    async def _new_guarded_page(
        self,
        *,
        proxy: Optional[Dict[str, str]],
        max_decoded_bytes: int,
        blocked_navigations: list[str],
    ) -> tuple[Any, Any, Any]:
        """Create one context whose HTTP traffic can only use the pinned fetcher."""
        transport = self._new_transport(proxy)
        context = None
        remaining_bytes = [max_decoded_bytes]
        try:
            context_options = _context_kwargs()
            if proxy is not None:
                context_options["proxy"] = proxy
            context = await self._browser.new_context(**context_options)

            async def guard_request(route):
                request = route.request
                if not request.url.startswith(("http://", "https://")):
                    await route.abort("blockedbyclient")
                    return
                body = getattr(request, "post_data_buffer", None)
                try:
                    response = await transport.fetch_request(
                        request.url, request.method, headers=dict(request.headers),
                        data=body, max_decoded_bytes=remaining_bytes[0],
                    )
                except UnsafeUrlError:
                    response = None
                if response is None:
                    await route.abort("blockedbyclient")
                    if request.is_navigation_request():
                        blocked_navigations.append(request.url)
                    return
                body = response["content"]
                if len(body) > remaining_bytes[0]:
                    await route.abort("blockedbyclient")
                    return
                remaining_bytes[0] -= len(body)
                headers = {
                    name: value for name, value in response["headers"].items()
                    if name.lower() not in {
                        "connection", "keep-alive", "proxy-authenticate",
                        "proxy-authorization", "te", "trailer",
                        "transfer-encoding", "upgrade", "content-encoding",
                        "content-length",
                    }
                }
                await route.fulfill(
                    status=response["status"], headers=headers, body=body,
                )

            await context.route("**/*", guard_request)
            await context.route_web_socket("**/*", _guard_browser_websocket)
            page = await context.new_page()
            try:
                await stealth_async(page)
            except Exception:
                pass
            return context, page, transport
        except Exception:
            if context is not None and hasattr(context, "close"):
                await context.close()
            await transport.close()
            raise

    async def open_owned_session(
        self,
        url: str,
        *,
        artifact_put: Callable[[bytes], Awaitable[str]],
        proxy: Optional[Dict[str, str]] = None,
        max_decoded_bytes: int = MAX_DOM_BYTES,
        artifact_budget_bytes: int = 20 * 1024 * 1024,
    ) -> Any:
        """Open a retained, guarded browser page for one human session.

        The returned context holds this runtime's context permit until it is
        closed, preventing an intervention session from bypassing render
        capacity.
        """
        from app.acquisition.owned_session import OwnedSessionContext

        await ensure_public_url(url)
        await self.start()
        await self._contexts.acquire()
        context = page = transport = None
        blocked_navigations: list[str] = []
        try:
            async with asyncio.timeout(90):
                context, page, transport = await self._new_guarded_page(
                    proxy=proxy,
                    max_decoded_bytes=max_decoded_bytes,
                    blocked_navigations=blocked_navigations,
                )
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            async def release() -> None:
                try:
                    await context.close()
                finally:
                    try:
                        await transport.close()
                    finally:
                        self._contexts.release()

            return OwnedSessionContext(
                context, page, artifact_put=artifact_put,
                artifact_budget_bytes=artifact_budget_bytes, release=release,
            )
        except Exception as exc:
            if context is not None and hasattr(context, "close"):
                await context.close()
            if transport is not None:
                await transport.close()
            self._contexts.release()
            if blocked_navigations:
                raise UnsafeUrlError(
                    "Refusing browser navigation to a non-public network address"
                ) from exc
            raise

    async def render(self, url: str, *, wait_for_ms: int = 1000,
                     actions: Optional[List[Dict[str, Any]]] = None,
                     capture_screenshot: bool = False,
                     max_dom_bytes: int = MAX_DOM_BYTES,
                     max_screenshot_bytes: int = MAX_SCREENSHOT_BYTES,
                     max_decoded_bytes: int = MAX_DOM_BYTES,
                     proxy: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Render one page in a fresh context, enforcing output-size limits."""
        await self.start()
        blocked_navigations = []
        context = None
        transport = None
        try:
            async with self._contexts:
                async with asyncio.timeout(90):
                    context, page, transport = await self._new_guarded_page(
                        proxy=proxy,
                        max_decoded_bytes=max_decoded_bytes,
                        blocked_navigations=blocked_navigations,
                    )
                    nav_response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=60000
                    )
                    status_code = nav_response.status if nav_response else None
                    if wait_for_ms > 0:
                        await asyncio.sleep(wait_for_ms / 1000.0)
                    action_outcomes = (
                        await page_actions.run_actions(page, actions) if actions else None
                    )
                    html_content = await page.content()
                    if len(html_content.encode("utf-8")) > max_dom_bytes:
                        raise ValueError("Rendered DOM exceeds byte limit")
                    captcha = None
                    captcha_blocked = False
                    policy = CaptchaPolicy.from_environment()
                    if policy.domains:
                        captcha_result = await solve_if_authorized(page, policy)
                        captcha = _captcha_metadata(captcha_result)
                        captcha_blocked = captcha_result.state in {
                            "requires_human_or_provider", "final_host_not_authorized",
                        }
                        if captcha_result.state == "submitted":
                            # One authorized submit may navigate; refresh only the
                            # bounded DOM and final URL before normal classification.
                            html_content = await page.content()
                            if len(html_content.encode("utf-8")) > max_dom_bytes:
                                raise ValueError("Rendered DOM exceeds byte limit")
                    # Preserve outcome ordering: challenge and HTTP failures are
                    # classified before title/metadata extraction or screenshots.
                    blocked_challenge = captcha_blocked or fetch.is_challenge_html(html_content)
                    if (blocked_challenge
                            or not isinstance(status_code, int)
                            or not 200 <= status_code < 300):
                        return {
                            "html": html_content,
                            "final_url": getattr(page, "url", url),
                            "status_code": status_code,
                            "title": "",
                            "description": "",
                            "screenshot": None,
                            "actions": action_outcomes,
                            "captcha": captcha,
                            "blocked_challenge": blocked_challenge,
                        }
                    screenshot = None
                    if capture_screenshot:
                        try:
                            screenshot = await page.screenshot(full_page=True)
                            if len(screenshot) > max_screenshot_bytes:
                                screenshot = None
                        except Exception:
                            screenshot = None
                    title = await page.title()
                    try:
                        description = await page.evaluate("""() => {
                            const meta = document.querySelector("meta[name='description']")
                                || document.querySelector("meta[property='og:description']");
                            return meta ? meta.getAttribute("content") || "" : "";
                        }""")
                    except Exception:
                        description = ""
                    return {
                        "html": html_content,
                        "final_url": getattr(page, "url", url),
                        "status_code": status_code,
                        "title": title,
                        "description": description.strip(),
                        "screenshot": screenshot,
                        "actions": action_outcomes,
                        "captcha": captcha,
                    }
        except Exception as exc:
            if blocked_navigations:
                raise UnsafeUrlError(
                    "Refusing browser navigation to a non-public network address"
                ) from exc
            raise
        finally:
            if context is not None and hasattr(context, "close"):
                await context.close()
            if transport is not None:
                await transport.close()
            self._completed_contexts += 1
            if self._completed_contexts >= self._restart_after:
                self._completed_contexts = 0
                await self.close()


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
        self.browser = BrowserRuntime()

    async def close(self) -> None:
        await self.browser.close()

    async def scrape(self, url: str, wait_for_ms: int = 1000, only_main_content: bool = True,
                     engine: str = "auto",
                     actions: Optional[List[Dict[str, Any]]] = None,
                     capture_screenshot: bool = True,
                     max_decoded_bytes: Optional[int] = None,
                     before_browser: Optional[Callable[[], Awaitable[bool]]] = None,
                     proxy: Optional[Dict[str, str]] = None,
                     trust_env: bool = False,
                     ) -> Dict[str, Any]:
        """Scrape a URL and convert to Markdown.

        engine: "auto" tries a cheap impersonated HTTP fetch first and only
        renders with Playwright when the response looks like a JS shell or a
        bot challenge; "http" / "browser" force a single tier. A non-empty
        `actions` list (wait/click/scroll/fill/press) forces the browser tier
        and runs between navigation and content capture.
        """
        if trust_env:
            raise ValueError("environment proxy settings are not permitted")
        if proxy is not None and (set(proxy) - {"server", "username", "password"}
                                  or not isinstance(proxy.get("server"), str)
                                  or ("username" in proxy) != ("password" in proxy)
                                  or any(not isinstance(proxy[key], str)
                                         for key in ("username", "password") if key in proxy)):
            raise ValueError("proxy must contain a server and optional credentials")
        engine = page_actions.effective_engine(engine, actions)
        fetch_options = {"proxy": proxy["server"]} if proxy else {}
        if proxy and "username" in proxy:
            fetch_options["proxy_auth"] = (proxy["username"], proxy["password"])
        # Tier 1: impersonated HTTP fetch, no browser
        if engine in ("auto", "http"):
            if max_decoded_bytes is None:
                resp = await fetch.fetch_http(url, **fetch_options)
            else:
                resp = await fetch.fetch_http(
                    url, max_decoded_bytes=max_decoded_bytes,
                    **fetch_options,
                )
            if resp is None:
                return self._error_result(
                    url, "HTTP fetch failed (transport error)",
                    reason="transport_error", engine_used="http",
                )
            status_code = resp.get("status")
            final_url = resp.get("final_url") or url
            if not _same_origin(url, final_url):
                return self._error_result(
                    url, "HTTP redirect crossed the crawl origin",
                    reason="policy_error", status_code=status_code, engine_used="http",
                )
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
                doc_result = await self._build_document_result(resp, final_url, kind)
                if doc_result:
                    if isinstance(doc_result.get("metadata"), dict):
                        doc_result["metadata"]["downloaded_bytes"] = len(
                            resp.get("content") or b""
                        )
                    return doc_result
                # Identified as a document but extraction failed (e.g. a scanned
                # PDF with no OCR available, or a corrupt EPUB). Do NOT fall
                # through to Playwright: chromium downloads documents instead of
                # rendering them ("Page.goto: Download is starting"), surfacing
                # as a 502. Degrade on the raw bytes here so the scrape still
                # succeeds (spec §11.5).
                result = self._build_result(
                    resp["html"], final_url, only_main_content,
                    engine_used="http", status_code=resp.get("status"),
                )
                if isinstance(result.get("metadata"), dict):
                    result["metadata"]["downloaded_bytes"] = len(
                        resp.get("content") or b""
                    )
                return result
            if engine == "http":
                result = self._build_result(
                    resp["html"], final_url, only_main_content,
                    engine_used="http", status_code=resp.get("status"),
                )
                if isinstance(result.get("metadata"), dict):
                    result["metadata"]["downloaded_bytes"] = len(
                        resp.get("content") or b""
                    )
                return result
            if not fetch.needs_browser(resp):
                result = self._build_result(
                    resp["html"], final_url, only_main_content,
                    engine_used="http", status_code=resp.get("status"),
                )
                if isinstance(result.get("metadata"), dict):
                    result["metadata"]["downloaded_bytes"] = len(
                        resp.get("content") or b""
                    )
                return result

        # Tier 2: full Playwright render
        await ensure_public_url(url)
        if before_browser is not None and not await before_browser():
            return self._error_result(
                url, "Browser page budget exhausted",
                reason="browser_budget_exhausted", engine_used="browser",
            )
        try:
            rendered = await self.browser.render(
                url, wait_for_ms=wait_for_ms, actions=actions,
                capture_screenshot=capture_screenshot,
                max_decoded_bytes=max_decoded_bytes or MAX_DOM_BYTES,
                proxy=proxy,
            )
            html_content = rendered["html"]
            final_url = rendered["final_url"]
            status_code = rendered["status_code"]
            captcha = rendered.get("captcha")
            if not _same_origin(url, final_url):
                return _attach_captcha_metadata(self._error_result(
                    url, "Browser redirect crossed the crawl origin",
                    reason="policy_error", status_code=status_code,
                    engine_used="browser",
                ), captcha)
            if rendered.get("blocked_challenge") or fetch.is_challenge_html(html_content):
                return _attach_captcha_metadata(self._error_result(
                    url, "Browser render returned a challenge page",
                    reason="blocked_challenge", status_code=status_code,
                    engine_used="browser",
                ), captcha)
            if not isinstance(status_code, int) or not 200 <= status_code < 300:
                return _attach_captcha_metadata(self._error_result(
                    url, f"Browser navigation failed (status {status_code})",
                    reason="http_status_error", status_code=status_code,
                    engine_used="browser",
                ), captcha)
            result = self._build_result(
                html_content, final_url, only_main_content,
                engine_used="browser", title=rendered["title"],
                description=rendered["description"], status_code=status_code,
            )
            if rendered["screenshot"]:
                result["_raw"]["screenshot"] = rendered["screenshot"]
            if rendered["actions"] is not None:
                result["metadata"]["actions"] = rendered["actions"]
            return _attach_captcha_metadata(result, captcha)
        except UnsafeUrlError:
            raise
        except Exception as e:
            return self._error_result(url, str(e))

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
            "discovery_html": html_content,
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
