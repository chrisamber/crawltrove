"""One-request Bright Data Web Unlocker acquisition adapter."""
import json
import logging
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.acquisition.providers import NativeCost, ProviderFailure, ProviderRequest, ProviderResult
from app.url_safety import UnsafeUrlError, ensure_public_url


logger = logging.getLogger(__name__)
MAX_HTML_BYTES = 10 * 1024 * 1024
_CHALLENGE_RE = re.compile(
    r"(?:captcha|recaptcha|hcaptcha|turnstile|cf-chl-|challenge-platform)", re.IGNORECASE,
)
_SAFE_RESPONSE_HEADERS = frozenset({"content-type", "content-length"})
MAX_RETRY_AFTER_SECONDS = 60 * 60


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Keep only harmless response metadata; billing/debug headers stay local."""
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in _SAFE_RESPONSE_HEADERS
    }


def _cost(requests: int = 1) -> NativeCost:
    return NativeCost({"requests": requests})


def _failure(
    code: str,
    retryable: bool,
    status_code: int | None = None,
    *,
    request_sent: bool = True,
    retry_after_seconds: int | None = None,
) -> ProviderFailure:
    return ProviderFailure(
        code, retryable, _cost(1 if request_sent else 0), status_code,
        retry_after_seconds,
    )


def _retry_after_seconds(value: str | None) -> int | None:
    """Parse bounded standard Retry-After values; malformed hints are ignored."""
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,10}", value):
        return min(int(value), MAX_RETRY_AFTER_SECONDS)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            return None
        seconds = math.ceil((retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return min(max(seconds, 0), MAX_RETRY_AFTER_SECONDS)


def _reported_payload(body: bytes, response_status: int) -> tuple[str, str | None, int | None]:
    """Return raw HTML and optional provider-reported target from either response shape."""
    text = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text, None, response_status
    if not isinstance(payload, Mapping):
        return text, None, response_status
    envelope: Mapping[str, Any] = payload
    if isinstance(payload.get("data"), Mapping):
        envelope = payload["data"]
    elif not any(key in payload for key in ("body", "html", "raw_html")):
        # A raw target can itself be JSON.  Only treat a recognized Bright Data
        # envelope as control data; otherwise preserve the target unchanged.
        return text, None, response_status
    html = next((envelope.get(key) for key in ("body", "html", "raw_html")
                 if isinstance(envelope.get(key), str)), None)
    if html is None:
        raise _failure("provider_protocol_error", False, response_status)
    final_url = next((envelope.get(key) for key in ("url", "final_url", "finalUrl")
                      if isinstance(envelope.get(key), str) and envelope.get(key)), None)
    status = next((envelope.get(key) for key in ("status_code", "statusCode", "status")
                   if isinstance(envelope.get(key), int)), response_status)
    return html, final_url, status


class BrightDataAdapter:
    """The fixed-meter Unlocker path; it deliberately exposes no vendor options."""

    name = "brightdata"
    routes = frozenset({"brightdata_unlocker"})

    def __init__(self, api_key: str | None, zone: str | None, *,
                 api_url: str = "https://api.brightdata.com/request",
                 transport: httpx.AsyncBaseTransport | None = None):
        self._api_key = (api_key or "").strip()
        self._zone = (zone or "").strip()
        self._api_url = api_url
        self._transport = transport

    def available(self) -> bool:
        return bool(self._api_key and self._zone)

    def reserve_cost(self, request: ProviderRequest) -> NativeCost:
        if request.route != "brightdata_unlocker":
            raise ValueError("Bright Data only serves brightdata_unlocker")
        return _cost()

    async def acquire(self, request: ProviderRequest) -> ProviderResult:
        if not self.available():
            raise _failure("provider_unavailable", False, request_sent=False)
        if request.route not in self.routes or request.timeout_seconds <= 0:
            raise _failure("provider_request", False, request_sent=False)
        # This is repeated from core admission deliberately: the provider call is
        # billable, so an adapter must not turn a bypassed admission check into one.
        try:
            await ensure_public_url(request.url)
        except UnsafeUrlError:
            raise _failure("unsafe_request_url", False, request_sent=False) from None
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload = {"zone": self._zone, "url": request.url, "format": "raw"}
        failure: ProviderFailure | None = None
        body = bytearray()
        status_code: int | None = None
        try:
            async with httpx.AsyncClient(
                transport=self._transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(request.timeout_seconds),
            ) as client:
                async with client.stream("POST", self._api_url, headers=headers, json=payload) as response:
                    status_code = response.status_code
                    if not 200 <= status_code < 300:
                        failure = self._classify_response(status_code, response.headers)
                    content_length = response.headers.get("content-length")
                    if failure is None and content_length and content_length.isdigit() and int(content_length) > MAX_HTML_BYTES:
                        failure = _failure("response_too_large", False, status_code)
                    if failure is None:
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > MAX_HTML_BYTES:
                                failure = _failure("response_too_large", False, status_code)
                                break
                            body.extend(chunk)
        except httpx.HTTPError:
            raise _failure("provider_transport", True) from None
        if failure is not None:
            raise failure
        assert status_code is not None
        html, reported_final, provider_status = _reported_payload(bytes(body), status_code)
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise _failure("response_too_large", False, status_code)
        if _CHALLENGE_RE.search(html):
            raise _failure("blocked_challenge", True, provider_status)
        final_url = reported_final or request.url
        if reported_final is None:
            logger.info("brightdata final_url_unreported")
        try:
            await ensure_public_url(final_url)
        except UnsafeUrlError:
            raise _failure("unsafe_final_url", False, provider_status) from None
        return ProviderResult(html, final_url, provider_status, _cost())

    @staticmethod
    def _classify_response(
        status_code: int, headers: Mapping[str, str],
    ) -> ProviderFailure:
        if status_code in (401, 403):
            return _failure("provider_auth", False, status_code)
        if status_code == 402:
            return _failure("provider_billing", False, status_code)
        if status_code == 429:
            return _failure(
                "provider_rate_limited", True, status_code,
                retry_after_seconds=_retry_after_seconds(headers.get("retry-after")),
            )
        if status_code >= 500:
            return _failure("provider_failure", True, status_code)
        return _failure("provider_request", False, status_code)

    async def cancel(self, remote_id: str) -> None:
        """Unlocker has no reusable session to cancel."""
        return None
