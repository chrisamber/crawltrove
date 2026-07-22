import asyncio
import hashlib
import json
import os
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, WebSocket, status
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.acquisition import sessions
from app.acquisition.owned_session import MAX_SCREENSHOT_BYTES, tunnel
from app.artifacts import FilesystemArtifactStore, S3ArtifactStore, artifact_store


router = APIRouter(prefix="/api/acquisition/sessions", tags=["acquisition"])
tunnel_router = APIRouter(prefix="/api/acquisition", tags=["acquisition"])


_CONTROL_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>CrawlTrove session</title><style>
body{font:14px system-ui;background:#101114;color:#eee;max-width:760px;margin:24px auto;padding:0 16px}
fieldset{border:1px solid #333;border-radius:8px;margin:12px 0;padding:12px}label{display:block;margin:8px 0}
input{width:100%;box-sizing:border-box;background:#181a1f;color:#fff;border:1px solid #444;padding:8px}
button{margin:4px;padding:8px 12px}img{display:block;max-width:100%;margin-top:12px;border:1px solid #333}
#status,#result{white-space:pre-wrap;word-break:break-word;color:#bbb}
</style></head><body><h1>Live acquisition session</h1>
<p id="status" role="status">Connecting…</p>
<fieldset><legend>Page controls</legend>
<button data-action="screenshot">Screenshot</button>
<label>CSS selector<input id="selector" maxlength="512" autocomplete="off"></label>
<label>Text<input id="text" maxlength="4096" autocomplete="off"></label>
<button data-action="click">Click</button><button data-action="fill">Fill</button>
<label>Key<input id="key" maxlength="64" value="Enter" autocomplete="off"></label>
<button data-action="press">Press key</button>
<button data-action="scroll" data-delta="600">Scroll down</button>
<button data-action="scroll" data-delta="-600">Scroll up</button></fieldset>
<fieldset><legend>Finish</legend><button data-action="resume">Resume crawl</button>
<button data-action="cancel">Cancel session</button></fieldset>
<div id="result" aria-live="polite"></div><img id="shot" alt="Current browser session screenshot" hidden>
<script>
history.replaceState(null,"",location.pathname);
const path=__CONTROL_PATH__,status=document.querySelector("#status"),result=document.querySelector("#result"),shot=document.querySelector("#shot");
const ws=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}${path}`);
ws.onopen=()=>status.textContent="Connected";ws.onclose=()=>status.textContent="Disconnected";
ws.onerror=()=>status.textContent="Connection error";
ws.onmessage=(event)=>{const message=JSON.parse(event.data);result.textContent=JSON.stringify(message.result||message.error||message);
const ref=message?.result?.screenshot?.artifactRef;if(ref&&(ref.startsWith("/")||ref.startsWith("https://")||ref.startsWith("http://"))){shot.src=ref;shot.hidden=false;}};
document.querySelectorAll("button[data-action]").forEach(button=>button.addEventListener("click",()=>{const action=button.dataset.action;
if((action==="resume"||action==="cancel")&&!confirm(`${action} this session?`))return;
const frame={action};if(action==="click"||action==="fill")frame.selector=document.querySelector("#selector").value;
if(action==="fill")frame.text=document.querySelector("#text").value;if(action==="press")frame.key=document.querySelector("#key").value;
if(action==="scroll")frame.delta=Number(button.dataset.delta);ws.send(JSON.stringify(frame));}));
</script></body></html>"""


class TokenRequest(BaseModel):
    scope: str = Field(pattern="^(view|control)$")
    ttlSeconds: int = Field(60, ge=1, le=3600)


def _unavailable(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/{session_id}")
async def get_session(session_id: UUID):
    try:
        snapshot = await sessions.inspect(session_id)
    except sessions.SessionPersistenceUnavailable as exc:
        raise _unavailable(exc) from exc
    if snapshot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    return {"status": snapshot.status, "expiresAt": snapshot.expires_at, "usage": snapshot.usage}


@router.post("/{session_id}/tokens")
async def create_token(session_id: UUID, request: TokenRequest):
    try:
        token = await sessions.issue_token(
            session_id, request.scope, ttl_seconds=request.ttlSeconds,
        )
    except sessions.SessionPersistenceUnavailable as exc:
        raise _unavailable(exc) from exc
    except sessions.SessionStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"token": token, "scope": request.scope}


@router.post("/{session_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_session(session_id: UUID):
    try:
        resumed = await sessions.request_resume(session_id)
    except sessions.SessionPersistenceUnavailable as exc:
        raise _unavailable(exc) from exc
    if not resumed:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Session is not resumable")
    return {"status": "resuming"}


@router.post("/{session_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_session(session_id: UUID):
    try:
        cancelled = await sessions.cancel(session_id)
    except sessions.SessionPersistenceUnavailable as exc:
        raise _unavailable(exc) from exc
    if not cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Session is already closed")
    return {"status": "cancelled"}


@router.get("/{session_id}/open")
async def open_session(session_id: UUID, token: str = Query(min_length=1, max_length=128)):
    """Exchange a one-use core token for a same-origin control endpoint only."""
    try:
        bridge = await tunnel.exchange_control(session_id, token)
        if bridge is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid session token")
        snapshot = await sessions.inspect(session_id)
    except sessions.SessionPersistenceUnavailable as exc:
        raise _unavailable(exc) from exc
    if snapshot is None or snapshot.status not in {"waiting", "connected"}:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Session is not available")
    # The bridge is a one-use core-local channel, not a provider URL or token.
    control_path = f"/api/acquisition/sessions/{session_id}/control?bridge={bridge}"
    response = HTMLResponse(
        _CONTROL_PAGE.replace("__CONTROL_PATH__", json.dumps(control_path)),
    )
    response.headers.update({
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self'"
        ),
    })
    return response


@router.get("/{session_id}/screenshots/{token}")
async def session_screenshot(session_id: UUID, token: str):
    digest = tunnel.consume_screenshot(session_id, token)
    if digest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Screenshot not found")
    try:
        worker_id = await sessions.worker_for_artifact(session_id)
        store = artifact_store(worker_id or os.getenv("CRAWLTROVE_WORKER_ID"))
    except (sessions.SessionPersistenceUnavailable, RuntimeError, ValueError) as exc:
        raise _unavailable(exc) from exc
    if isinstance(store, FilesystemArtifactStore):
        path = store.root / "sha256" / digest[:2] / digest
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Screenshot not found") from exc
    elif isinstance(store, S3ArtifactStore):
        scoped_worker = worker_id or store.worker_id
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", scoped_worker):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Screenshot not found")
        key = f"workers/{scoped_worker}/sha256/{digest[:2]}/{digest}"
        try:
            response = await asyncio.to_thread(
                store.client.get_object, Bucket=store.bucket, Key=key,
            )
            if response.get("ContentLength", MAX_SCREENSHOT_BYTES + 1) > MAX_SCREENSHOT_BYTES:
                raise ValueError("screenshot exceeds byte limit")
            body = response["Body"]
            try:
                data = await asyncio.to_thread(body.read, MAX_SCREENSHOT_BYTES + 1)
            finally:
                await asyncio.to_thread(body.close)
        except Exception as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Screenshot not found") from exc
    else:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Artifact store unavailable")
    if len(data) > MAX_SCREENSHOT_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Screenshot not found")
    return Response(
        content=data, media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@tunnel_router.websocket("/tunnel/{session_id}")
async def outbound_worker_tunnel(websocket: WebSocket, session_id: UUID):
    authorization = websocket.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    await tunnel.worker(websocket, session_id, token)


@router.websocket("/{session_id}/control")
async def control_tunnel(websocket: WebSocket, session_id: UUID, bridge: str = Query(min_length=1, max_length=128)):
    await tunnel.control(websocket, session_id, bridge)
