from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.acquisition import sessions


router = APIRouter(prefix="/api/acquisition/sessions", tags=["acquisition"])


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
