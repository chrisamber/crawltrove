"""Narrow authenticated image/text OCR endpoint for dedicated solver workers."""
from __future__ import annotations

import asyncio
import base64
import binascii
import os
import secrets
from typing import Callable

from fastapi import FastAPI, HTTPException, Request

from app.acquisition.captcha import (
    CaptchaPolicy,
    MAX_IMAGE_BYTES,
    _answer,
    _ocr_image,
    load_solver_token,
)


def create_app(
    *,
    token: str,
    policy: CaptchaPolicy,
    ocr: Callable[[bytes], tuple[str | None, float]] = _ocr_image,
) -> FastAPI:
    """Create a service that accepts only an authorized bounded image payload."""
    if not token:
        raise ValueError("captcha service token is required")
    app = FastAPI(title="CrawlTrove CAPTCHA Solver", docs_url=None, redoc_url=None)

    @app.post("/solve")
    async def solve(request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or not secrets.compare_digest(
            authorization[7:], token
        ):
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="invalid JSON payload") from exc
        if not isinstance(payload, dict) or set(payload) != {"kind", "host", "imageBase64"}:
            raise HTTPException(status_code=422, detail="unsupported CAPTCHA payload")
        if payload.get("kind") != "image_text" or not isinstance(payload.get("host"), str):
            raise HTTPException(status_code=422, detail="unsupported CAPTCHA challenge")
        if not policy.allows(payload["host"]):
            raise HTTPException(status_code=403, detail="host is not authorized")
        encoded = payload.get("imageBase64")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise HTTPException(status_code=422, detail="image exceeds limit")
        try:
            image = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise HTTPException(status_code=422, detail="image is not base64") from exc
        if len(image) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=422, detail="image exceeds limit")
        try:
            answer, confidence = await asyncio.to_thread(ocr, image)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="image is invalid") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="OCR is unavailable") from exc
        try:
            confidence_value = max(0.0, min(float(confidence), 1.0))
        except (TypeError, ValueError):
            confidence_value = 0.0
        return {"answer": _answer(answer), "confidence": confidence_value}

    return app


def create_app_from_environment() -> FastAPI:
    """Build the dedicated service from the same private token file as its client."""
    token_file = os.environ.get("CAPTCHA_SOLVER_TOKEN_FILE", "").strip()
    if not token_file:
        app = FastAPI(title="CrawlTrove CAPTCHA Solver", docs_url=None, redoc_url=None)

        @app.post("/solve")
        async def unavailable():
            raise HTTPException(status_code=503, detail="CAPTCHA solver is not configured")

        return app
    return create_app(
        token=load_solver_token(token_file),
        policy=CaptchaPolicy.from_environment(),
    )


app = create_app_from_environment()
