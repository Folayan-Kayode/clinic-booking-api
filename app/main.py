"""HTTP layer for the clinic booking API.

Every endpoint is JSON. Every failure is {"error": "<message>"}.
Every request needs the X-API-Key header (level 2).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import logic
from .db import database
from .errors import ApiError

log = logging.getLogger("booking-api")

API_KEY = (os.environ.get("API_KEY") or "").strip()
STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_PATHS = {"/", "/health", "/favicon.ico"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not API_KEY:
        raise RuntimeError("API_KEY environment variable is required")
    database.open()
    log.info("booking api ready")
    yield
    database.close()


app = FastAPI(
    title="Clinic booking API",
    version="1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# ---------------------------------------------------------------------------
# level 2 - API key
# ---------------------------------------------------------------------------
@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    provided = request.headers.get("x-api-key") or ""
    if not API_KEY or not secrets.compare_digest(provided, API_KEY):
        return JSONResponse(
            {"error": "unauthorized: missing or invalid X-API-Key"}, status_code=401
        )
    return await call_next(request)


# Registered after the API-key middleware so 401 responses carry CORS headers too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
@app.exception_handler(ApiError)
async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status, headers=exc.headers)


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "request failed"
    return JSONResponse({"error": message}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(Exception)
async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error: %s", exc)
    return JSONResponse({"error": "internal server error"}, status_code=500)


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "request body must be valid JSON") from None
    if not isinstance(payload, dict):
        raise ApiError(400, "request body must be a JSON object")
    return payload


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "database": database.dialect}


@app.post("/reset")
async def reset(request: Request) -> JSONResponse:
    payload = await _json_body(request)
    return JSONResponse(await run_in_threadpool(logic.replace_slots, payload))


@app.get("/slots")
async def slots(request: Request) -> JSONResponse:
    result = await run_in_threadpool(
        logic.list_slots, request.query_params.get("provider")
    )
    return JSONResponse(result)


@app.post("/bookings")
async def create_booking(request: Request) -> JSONResponse:
    payload = await _json_body(request)
    status, body, headers = await run_in_threadpool(
        logic.create_booking, payload, request.headers.get("idempotency-key")
    )
    return JSONResponse(body, status_code=status, headers=headers)


@app.get("/bookings")
async def list_bookings(request: Request) -> JSONResponse:
    params = request.query_params
    result = await run_in_threadpool(
        logic.list_bookings,
        params.get("email"),
        params.get("limit"),
        params.get("cursor"),
    )
    return JSONResponse(result)


@app.get("/bookings/{booking_id}")
async def get_booking(booking_id: str) -> JSONResponse:
    return JSONResponse(await run_in_threadpool(logic.get_booking, booking_id))


@app.delete("/bookings/{booking_id}")
async def cancel_booking(booking_id: str) -> JSONResponse:
    return JSONResponse(await run_in_threadpool(logic.cancel_booking, booking_id))


# ---------------------------------------------------------------------------
# web page
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__API_KEY_JSON__", json.dumps(API_KEY)))
