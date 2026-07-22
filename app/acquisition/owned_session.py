"""Bounded same-origin relay for owned human-intervention browser sessions."""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from typing import Any, Awaitable, Callable, Literal, Mapping
from uuid import UUID
from urllib.parse import urlsplit, urlunsplit

from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from app.acquisition import sessions
from app.crawl.config import CrawlConfig
from app.crawl.discovery import discover_links
from app.crawl.types import ClaimedTask, TaskResult
from app.url_safety import ensure_public_url

MAX_FRAME_BYTES = 16 * 1024
ACTION_DEADLINE_SECONDS = 30
MAX_ARTIFACT_REF_LENGTH = 2048
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
_ACTIONS = frozenset({"screenshot", "click", "fill", "press", "scroll", "resume", "cancel", "ping"})
OwnedAction = Literal["screenshot", "click", "fill", "press", "scroll", "resume", "cancel", "ping"]


def validate_action(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Allow only small declarative browser actions; never arbitrary code/CDP."""
    if not isinstance(frame, Mapping) or set(frame) - {"action", "selector", "text", "key", "delta"}:
        raise ValueError("invalid session action")
    action = frame.get("action")
    if action not in _ACTIONS:
        raise ValueError("unsupported session action")
    for name, limit in (("selector", 512), ("text", 4096), ("key", 64)):
        value = frame.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError("invalid session action value")
    if action in {"click", "fill"} and not isinstance(frame.get("selector"), str):
        raise ValueError("selector required")
    if action == "fill" and not isinstance(frame.get("text"), str):
        raise ValueError("text required")
    if action == "press" and not isinstance(frame.get("key"), str):
        raise ValueError("key required")
    if action == "scroll" and (not isinstance(frame.get("delta"), int) or abs(frame["delta"]) > 10_000):
        raise ValueError("bounded scroll delta required")
    return dict(frame)


def _valid_worker_result(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Accept only the small reply shapes the control client understands."""
    if set(result) == {"ok"} and result.get("ok") is True:
        return {"ok": True}
    if set(result) == {"pong"} and result.get("pong") is True:
        return {"pong": True}
    if set(result) != {"screenshot"}:
        return None
    screenshot = result.get("screenshot")
    if not isinstance(screenshot, Mapping) or set(screenshot) != {"artifactRef"}:
        return None
    ref = screenshot.get("artifactRef")
    if not isinstance(ref, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", ref) is None:
        return None
    return {"screenshot": {"artifactRef": ref}}


class OwnedSessionTunnel:
    """Relay core control frames to an outbound worker WSS connection."""

    def __init__(self) -> None:
        self._workers: dict[UUID, WebSocket] = {}
        self._local: dict[UUID, OwnedSessionContext] = {}
        self._bridges: dict[str, tuple[UUID, float]] = {}
        self._screenshots: dict[str, tuple[UUID, str, float]] = {}
        self._pending: dict[
            tuple[UUID, str], tuple[WebSocket, asyncio.Future[dict[str, Any]]]
        ] = {}

    def attach_local(self, session_id: UUID, context: OwnedSessionContext) -> None:
        self._local[session_id] = context

    def detach_local(self, session_id: UUID, context: OwnedSessionContext) -> None:
        if self._local.get(session_id) is context:
            self._local.pop(session_id, None)

    async def exchange_control(self, session_id: UUID, token: str) -> str | None:
        if not await sessions.consume_token(session_id, token, "control"):
            return None
        now = time.monotonic()
        self._bridges = {
            key: value for key, value in self._bridges.items() if value[1] > now
        }
        if len(self._bridges) >= 1024:
            self._bridges.pop(next(iter(self._bridges)))
        bridge = secrets.token_urlsafe(24)
        self._bridges[bridge] = (session_id, now + 60)
        return bridge

    def consume_bridge(self, session_id: UUID, bridge: str) -> bool:
        value = self._bridges.pop(bridge, None)
        return value is not None and value[0] == session_id and value[1] > time.monotonic()

    def expose_screenshot(self, session_id: UUID, result: dict[str, Any]) -> dict[str, Any]:
        screenshot = result.get("screenshot")
        if not isinstance(screenshot, Mapping):
            return result
        ref = screenshot.get("artifactRef")
        match = re.fullmatch(r"sha256:([0-9a-f]{64})", ref or "")
        if match is None:
            raise ValueError("invalid screenshot artifact reference")
        now = time.monotonic()
        self._screenshots = {
            key: value for key, value in self._screenshots.items() if value[2] > now
        }
        if len(self._screenshots) >= 1024:
            self._screenshots.pop(next(iter(self._screenshots)))
        token = secrets.token_urlsafe(24)
        self._screenshots[token] = (session_id, match.group(1), now + 60)
        return {
            "screenshot": {
                "artifactRef": f"/api/acquisition/sessions/{session_id}/screenshots/{token}",
            },
        }

    def consume_screenshot(self, session_id: UUID, token: str) -> str | None:
        value = self._screenshots.pop(token, None)
        if value is None or value[0] != session_id or value[2] <= time.monotonic():
            return None
        return value[1]

    def _drop_pending(self, session_id: UUID, worker: WebSocket, error: Exception) -> None:
        for key, (pending_worker, future) in list(self._pending.items()):
            if key[0] == session_id and pending_worker is worker:
                self._pending.pop(key, None)
                if not future.done():
                    future.set_exception(error)

    async def worker(self, websocket: WebSocket, session_id: UUID, token: str) -> None:
        if not await sessions.consume_token(session_id, token, "worker"):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        self._workers[session_id] = websocket
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode()) > MAX_FRAME_BYTES:
                    await websocket.close(code=1009)
                    return
                payload = json.loads(raw)
                request_id = payload.get("id") if isinstance(payload, Mapping) else None
                result = payload.get("result") if isinstance(payload, Mapping) else None
                if not isinstance(request_id, str) or len(request_id) > 64 or not isinstance(result, Mapping):
                    await websocket.close(code=1008)
                    return
                bounded_result = _valid_worker_result(result)
                if bounded_result is None:
                    await websocket.close(code=1008)
                    return
                bounded_result = self.expose_screenshot(session_id, bounded_result)
                pending = self._pending.pop((session_id, request_id), None)
                if pending is not None and pending[0] is websocket and not pending[1].done():
                    pending[1].set_result(bounded_result)
                await sessions.touch(session_id)
        except (WebSocketDisconnect, ValueError, json.JSONDecodeError):
            return
        finally:
            if self._workers.get(session_id) is websocket:
                self._workers.pop(session_id, None)
            self._drop_pending(session_id, websocket, ConnectionError("worker disconnected"))

    async def control(self, websocket: WebSocket, session_id: UUID, bridge: str) -> None:
        if not self.consume_bridge(session_id, bridge):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode()) > MAX_FRAME_BYTES:
                    await websocket.close(code=1009)
                    return
                action = validate_action(json.loads(raw))
                local = self._local.get(session_id)
                if local is not None:
                    if action["action"] == "cancel":
                        await sessions.cancel(session_id)
                        result = {"ok": True}
                    elif action["action"] == "resume":
                        await sessions.request_resume(session_id)
                        result = {"ok": True}
                    else:
                        result = await local.execute(action)
                        await sessions.touch(session_id)
                    result = self.expose_screenshot(session_id, result)
                    await websocket.send_json({"result": result})
                    continue
                worker = self._workers.get(session_id)
                if worker is None:
                    await websocket.send_json({"error": "worker_unavailable"})
                    continue
                request_id = secrets.token_urlsafe(12)
                future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
                key = (session_id, request_id)
                self._pending[key] = (worker, future)
                try:
                    await asyncio.wait_for(
                        worker.send_json({"id": request_id, **action}), ACTION_DEADLINE_SECONDS,
                    )
                    if action["action"] == "cancel":
                        await sessions.cancel(session_id)
                    elif action["action"] == "resume":
                        await sessions.request_resume(session_id)
                    await sessions.touch(session_id)
                    result = await asyncio.wait_for(future, ACTION_DEADLINE_SECONDS)
                    await websocket.send_json({"id": request_id, "result": result})
                finally:
                    pending = self._pending.pop(key, None)
                    if pending is not None and not pending[1].done():
                        pending[1].cancel()
        except (WebSocketDisconnect, ValueError, json.JSONDecodeError, asyncio.TimeoutError):
            return


tunnel = OwnedSessionTunnel()


class OwnedSessionContext:
    """One retained, fresh Playwright context with no script-evaluation escape."""

    def __init__(
        self,
        context: Any,
        page: Any,
        *,
        artifact_put: Callable[[bytes], Awaitable[str]],
        artifact_budget_bytes: int = MAX_SCREENSHOT_BYTES,
        release: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.context, self.page, self._artifact_put = context, page, artifact_put
        self._release = release or context.close
        self._artifact_bytes_remaining = max(0, int(artifact_budget_bytes))
        self.closed = False
        self._action_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    async def execute(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        action = validate_action(frame)
        async with self._action_lock:
            if self.closed:
                raise RuntimeError("session context is closed")
            kind = action["action"]
            if kind == "click":
                await self.page.click(action["selector"], timeout=ACTION_DEADLINE_SECONDS * 1000)
            elif kind == "fill":
                await self.page.fill(action["selector"], action["text"], timeout=ACTION_DEADLINE_SECONDS * 1000)
            elif kind == "press":
                await self.page.keyboard.press(action["key"])
            elif kind == "scroll":
                await self.page.mouse.wheel(0, action["delta"])
            elif kind == "screenshot":
                screenshot = await self.page.screenshot()
                if (not isinstance(screenshot, bytes) or len(screenshot) > MAX_SCREENSHOT_BYTES
                        or len(screenshot) > self._artifact_bytes_remaining):
                    raise ValueError("session screenshot exceeds byte budget")
                ref = await self._artifact_put(screenshot)
                if not isinstance(ref, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", ref) is None:
                    raise ValueError("invalid session screenshot artifact reference")
                self._artifact_bytes_remaining -= len(screenshot)
                return {"screenshot": {"artifactRef": ref}}
            elif kind == "ping":
                return {"pong": True}
            return {"ok": True}

    async def resume(self, scraper: Any) -> Mapping[str, Any]:
        async with self._action_lock:
            try:
                if self.closed:
                    raise RuntimeError("session context is closed")
                final_url = self.page.url
                await ensure_public_url(final_url)
                html = await self.page.content()
                if len(html.encode("utf-8")) > 10 * 1024 * 1024:
                    raise ValueError("session DOM exceeds limit")
                return scraper._build_result(
                    html, final_url, True, engine_used="browser", status_code=None,
                )
            finally:
                await self.close()

    async def close(self) -> None:
        async with self._close_lock:
            if self.closed:
                return
            self.closed = True
            await self._release()


class OwnedSessionRunner:
    """Retain owned contexts outside claim capacity until resume or closure."""

    def __init__(
        self,
        worker_id: str,
        repository: Any,
        scraper: Any,
        artifacts: Any,
        *,
        core_url: str | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.repository = repository
        self.scraper = scraper
        self.artifacts = artifacts
        self.core_url = core_url.rstrip("/") if core_url else None
        self._connect = connect
        self._contexts: dict[UUID, OwnedSessionContext] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    async def _artifact_put(self, data: bytes) -> str:
        async def chunks():
            yield data

        ref = await self.artifacts.put(chunks(), "image/png", MAX_SCREENSHOT_BYTES)
        return f"sha256:{ref.sha256}"

    async def start(self, task: ClaimedTask, scrape_options: Mapping[str, Any]) -> bool:
        before_browser = scrape_options.get("before_browser")
        if callable(before_browser) and not await before_browser():
            return False
        context = await self.scraper.browser.open_owned_session(
            task.url,
            artifact_put=self._artifact_put,
            proxy=scrape_options.get("proxy"),
            max_decoded_bytes=task.byte_allowance,
            artifact_budget_bytes=task.artifact_allowance,
        )
        try:
            handle = await self.repository.start_live_session(
                task, backend="owned", worker_id=self.worker_id,
            )
            if handle is None:
                return False
            self._contexts[handle.id] = context
            if self.core_url is None:
                tunnel.attach_local(handle.id, context)
                running = asyncio.create_task(self._local_loop(handle.id, task, context))
            else:
                running = asyncio.create_task(self._remote_loop(handle.id, task, context))
            self._tasks[handle.id] = running
            def forget(
                _done: asyncio.Future[None], session_id: UUID = handle.id,
            ) -> None:
                self._forget(session_id)

            running.add_done_callback(forget)
            return True
        finally:
            if not any(value is context for value in self._contexts.values()):
                await context.close()

    def _forget(self, session_id: UUID) -> None:
        context = self._contexts.pop(session_id, None)
        self._tasks.pop(session_id, None)
        if context is not None:
            tunnel.detach_local(session_id, context)

    async def _state(self, session_id: UUID) -> str | None:
        inspect = getattr(self.repository, "inspect_live_session", None)
        if inspect is not None:
            value = await inspect(session_id)
            return value.get("state") if isinstance(value, Mapping) else None
        snapshot = await sessions.inspect(session_id)
        return snapshot.status if snapshot is not None else None

    async def _complete(
        self, session_id: UUID, task: ClaimedTask, context: OwnedSessionContext,
    ) -> None:
        built = await context.resume(self.scraper)
        final_url = str(built.get("url") or task.url)
        config = CrawlConfig.model_validate(task.config)
        html = str(built.get("discovery_html") or "")
        links = discover_links(html, final_url, config)
        metadata = dict(built.get("metadata") or {})
        metadata["downloaded_bytes"] = len(html.encode("utf-8"))
        result = TaskResult(
            final_url=final_url,
            status_code=metadata.get("status_code"),
            title=str(built.get("title") or ""),
            markdown=str(built.get("markdown") or ""),
            metadata=metadata,
            discovered_urls=tuple(link.url for link in links),
        )
        while await self._state(session_id) == "resuming":
            claim = await self.repository.resume_live_session(session_id, self.worker_id)
            if claim is None:
                await asyncio.sleep(0.25)
                continue
            if not await self.repository.complete_task(claim.id, claim.lease_token, result):
                raise RuntimeError("live session completion lost its fence")
            finish = getattr(self.repository, "finish_live_session", None)
            if finish is not None:
                await finish(session_id)
            else:
                await sessions.close_completed(session_id)
            return

    async def _close_session(self, session_id: UUID, reason: str) -> None:
        close_session = getattr(self.repository, "close_live_session", None)
        if close_session is not None:
            await close_session(session_id, reason, self.worker_id)
        else:
            await sessions.close(session_id, reason, worker_id=self.worker_id)

    async def _local_loop(
        self, session_id: UUID, task: ClaimedTask, context: OwnedSessionContext,
    ) -> None:
        try:
            while True:
                state = await self._state(session_id)
                if state == "resuming":
                    await self._complete(session_id, task, context)
                    return
                if state not in {"waiting", "connected"}:
                    return
                await asyncio.sleep(0.25)
        except Exception:
            await self._close_session(session_id, "expired")
        finally:
            await context.close()

    def _worker_tunnel_url(self, session_id: UUID) -> str:
        parsed = urlsplit(self.core_url or "")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("core URL cannot contain credentials, query, or fragment")
        if parsed.scheme in {"https", "wss"}:
            scheme = "wss"
        elif (parsed.scheme in {"http", "ws"}
              and os.environ.get("WORKER_ALLOW_INSECURE_CORE", "").lower()
              in {"1", "true", "yes", "on"}):
            scheme = "ws"
        else:
            raise ValueError("remote session core URL requires WSS")
        if not parsed.hostname:
            raise ValueError("remote session core URL requires a host")
        return urlunsplit((scheme, parsed.netloc, f"/api/acquisition/tunnel/{session_id}", "", ""))

    async def _serve_remote(
        self, websocket: Any, session_id: UUID, task: ClaimedTask,
        context: OwnedSessionContext,
    ) -> bool:
        while True:
            state = await self._state(session_id)
            if state == "resuming":
                await self._complete(session_id, task, context)
                return True
            if state not in {"waiting", "connected"}:
                return True
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not isinstance(raw, str) or len(raw.encode()) > MAX_FRAME_BYTES:
                raise ValueError("invalid core session frame")
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                raise ValueError("invalid core session frame")
            request_id = payload.get("id")
            if not isinstance(request_id, str) or not request_id or len(request_id) > 64:
                raise ValueError("invalid core session request ID")
            action = validate_action({key: value for key, value in payload.items() if key != "id"})
            if action["action"] == "resume":
                result = {"ok": True}
            elif action["action"] == "cancel":
                await context.close()
                result = {"ok": True}
            else:
                result = await context.execute(action)
            await websocket.send(json.dumps({"id": request_id, "result": result}, separators=(",", ":")))

    async def _remote_loop(
        self, session_id: UUID, task: ClaimedTask, context: OwnedSessionContext,
    ) -> None:
        try:
            connect_factory = self._connect
            if connect_factory is None:
                from websockets.asyncio.client import connect as websocket_connect

                connect_factory = websocket_connect
            url = self._worker_tunnel_url(session_id)
            while True:
                state = await self._state(session_id)
                if state == "resuming":
                    await self._complete(session_id, task, context)
                    return
                if state not in {"waiting", "connected"}:
                    return
                token = await self.repository.issue_live_session_token(session_id)
                if not token:
                    await asyncio.sleep(1)
                    continue
                try:
                    async with connect_factory(
                        url, additional_headers={"Authorization": f"Bearer {token}"},
                        max_size=MAX_FRAME_BYTES,
                    ) as websocket:
                        if await self._serve_remote(websocket, session_id, task, context):
                            return
                except (ConnectionClosed, ConnectionError, OSError, asyncio.TimeoutError):
                    await asyncio.sleep(1)
        except Exception:
            await self._close_session(session_id, "expired")
        finally:
            await context.close()

    async def close(self) -> None:
        session_ids = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close_session = getattr(self.repository, "close_live_session", None)
        if close_session is not None and session_ids:
            await asyncio.gather(*(
                close_session(session_id, "expired", self.worker_id)
                for session_id in session_ids
            ), return_exceptions=True)
        contexts = list(self._contexts.values())
        self._contexts.clear()
        self._tasks.clear()
        for context in contexts:
            await context.close()
