"""Business rules for the clinic booking API (levels 1-5)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import database
from .errors import ApiError

# Level 5: at most 5 attempts per email per minute.
RATE_LIMIT_MAX = max(1, int(os.environ.get("RATE_LIMIT_MAX", "5")))
RATE_LIMIT_WINDOW_SECONDS = max(1, int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60")))

# A half-finished idempotent request older than this is considered abandoned.
PENDING_TIMEOUT_SECONDS = 30
PENDING_WAIT_SECONDS = 3.0

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)

STORAGE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------
def parse_datetime(value: Any) -> datetime | None:
    """Accept ISO-8601 with Z or an offset; assume UTC when naive."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def store_time(value: datetime) -> str:
    """Fixed-width UTC string; lexicographic order == chronological order."""
    return value.astimezone(timezone.utc).strftime(STORAGE_FORMAT)


def read_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def output_time(value: str | datetime) -> str:
    moment = read_time(value) if isinstance(value, str) else value
    return (
        moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{moment.microsecond // 1000:03d}Z"
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# serialisation helpers
# ---------------------------------------------------------------------------
def booking_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": output_time(row["created_at"]),
    }


def slot_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "provider": row["provider"],
        "start": row["start_raw"],
        "end": row["end_raw"],
        "available": not bool(row["booked"]),
    }


def encode_cursor(created_at: str, booking_id: str) -> str:
    raw = json.dumps([created_at, booking_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: Any) -> tuple[str, str] | None:
    if cursor is None or not str(cursor).strip():
        return None
    text = str(cursor).strip()
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ApiError(400, "invalid cursor") from None
    if (
        not isinstance(payload, list)
        or len(payload) != 2
        or not all(isinstance(part, str) for part in payload)
    ):
        raise ApiError(400, "invalid cursor")
    try:
        read_time(payload[0])
    except ValueError:
        raise ApiError(400, "invalid cursor") from None
    return payload[0], payload[1]


# ---------------------------------------------------------------------------
# Level 1 - reset / slots
# ---------------------------------------------------------------------------
def replace_slots(payload: dict[str, Any]) -> dict[str, Any]:
    slots = payload.get("slots")
    if not isinstance(slots, list):
        raise ApiError(400, "slots must be an array")

    parsed: list[tuple[str, str, str, str, str, str]] = []
    for item in slots:
        if not isinstance(item, dict):
            raise ApiError(400, "each slot must be an object")
        slot_id = _text(item.get("id")).strip()
        provider = _text(item.get("provider")).strip()
        start_raw = _text(item.get("start"))
        end_raw = _text(item.get("end"))
        if not slot_id or not provider or not start_raw or not end_raw:
            raise ApiError(400, "each slot needs id, provider, start and end")
        start = parse_datetime(start_raw)
        end = parse_datetime(end_raw)
        if start is None or end is None:
            raise ApiError(400, f"slot {slot_id} has an invalid start or end")
        parsed.append((slot_id, provider, store_time(start), start_raw, store_time(end), end_raw))

    # Duplicate ids in one payload: the last definition wins.
    deduped: dict[str, tuple[str, str, str, str, str, str]] = {}
    for row in parsed:
        deduped[row[0]] = row

    with database.connect() as conn, conn.tx():
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM idempotency_keys")
        conn.execute("DELETE FROM booking_attempts")
        conn.execute("DELETE FROM slots")
        for row in deduped.values():
            conn.execute(
                "INSERT INTO slots (id, provider, start_at, start_raw, end_at, end_raw) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )

    return {"ok": True, "slots": len(deduped)}


def list_slots(provider: Any = None) -> dict[str, Any]:
    sql = (
        "SELECT s.id, s.provider, s.start_raw, s.end_raw, "
        "  CASE WHEN EXISTS ("
        "    SELECT 1 FROM bookings b WHERE b.slot_id = s.id AND b.status = 'confirmed'"
        "  ) THEN 1 ELSE 0 END AS booked "
        "FROM slots s"
    )
    params: list[Any] = []
    provider_text = _text(provider).strip() if provider is not None else ""
    if provider_text:
        sql += " WHERE s.provider = ?"
        params.append(provider_text)
    sql += " ORDER BY s.start_at ASC, s.id ASC"

    with database.connect() as conn:
        rows = conn.execute(sql, params).all()
    return {"slots": [slot_json(row) for row in rows]}


# ---------------------------------------------------------------------------
# Level 5 - rate limit
# ---------------------------------------------------------------------------
def _check_rate_limit(email: str) -> None:
    now = utcnow()
    cutoff = store_time(now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS))
    with database.connect() as conn, conn.tx():
        if conn.dialect == "postgres":
            # Serialise concurrent attempts for the same email.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (email,))
        conn.execute(
            "DELETE FROM booking_attempts WHERE email = ? AND attempted_at < ?",
            (email, cutoff),
        )
        attempts = conn.execute(
            "SELECT attempted_at FROM booking_attempts WHERE email = ? "
            "ORDER BY attempted_at ASC",
            (email,),
        ).all()
        if len(attempts) >= RATE_LIMIT_MAX:
            oldest = read_time(attempts[0]["attempted_at"])
            remaining = RATE_LIMIT_WINDOW_SECONDS - (now - oldest).total_seconds()
            retry_after = max(1, math.ceil(remaining))
            raise ApiError(
                429,
                "too many booking attempts for this email, try again later",
                headers={"Retry-After": str(retry_after)},
            )
        conn.execute(
            "INSERT INTO booking_attempts (email, attempted_at) VALUES (?, ?)",
            (email, store_time(now)),
        )


# ---------------------------------------------------------------------------
# Level 4 - idempotency
# ---------------------------------------------------------------------------
def _body_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {field: _text(payload.get(field)) for field in ("slot_id", "name", "email")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _idempotency_reserve(key: str, body_hash: str) -> bool:
    with database.connect() as conn, conn.tx():
        result = conn.execute(
            "INSERT INTO idempotency_keys (key, body_hash, status, response, created_at) "
            "VALUES (?, ?, NULL, NULL, ?) ON CONFLICT (key) DO NOTHING",
            (key, body_hash, store_time(utcnow())),
        )
        return result.rowcount == 1


def _idempotency_store(key: str, status: int, body: dict[str, Any]) -> None:
    with database.connect() as conn, conn.tx():
        conn.execute(
            "UPDATE idempotency_keys SET status = ?, response = ? WHERE key = ?",
            (status, json.dumps(body), key),
        )


def _idempotency_release(key: str) -> None:
    with database.connect() as conn, conn.tx():
        conn.execute(
            "DELETE FROM idempotency_keys WHERE key = ? AND status IS NULL", (key,)
        )


def _idempotency_claim(key: str, body_hash: str) -> tuple[int, dict[str, Any], dict[str, str]] | None:
    """Return a stored response for a replay, or None once we own the key."""
    deadline = time.monotonic() + PENDING_WAIT_SECONDS
    while True:
        with database.connect() as conn:
            row = conn.execute(
                "SELECT body_hash, status, response, created_at "
                "FROM idempotency_keys WHERE key = ?",
                (key,),
            ).first()

        if row is None:
            if _idempotency_reserve(key, body_hash):
                return None
        elif row["body_hash"] != body_hash:
            raise ApiError(422, "idempotency key reused with a different request body")
        elif row["status"] is not None:
            try:
                body = json.loads(row["response"]) if row["response"] else {}
            except ValueError:
                body = {}
            return int(row["status"]), body, {}
        elif (utcnow() - read_time(row["created_at"])).total_seconds() > PENDING_TIMEOUT_SECONDS:
            # The request that reserved this key died before finishing.
            _idempotency_release(key)
            continue

        if time.monotonic() > deadline:
            raise ApiError(409, "a request with this idempotency key is still in progress")
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Level 1-3 - create booking
# ---------------------------------------------------------------------------
def _is_unique_violation(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return "unique" in str(exc).lower()
    if exc.__class__.__name__ == "UniqueViolation":
        return True
    return getattr(exc, "sqlstate", None) == "23505"


def _perform_booking(slot_id: str, name: str, email: str) -> tuple[int, dict[str, Any], dict[str, str]]:
    if not slot_id or not name or not email:
        raise ApiError(400, "slot_id, name and email are required")
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ApiError(400, "email is not valid")

    with database.connect() as conn:
        slot = conn.execute(
            "SELECT id, start_at FROM slots WHERE id = ?", (slot_id,)
        ).first()
    if slot is None:
        raise ApiError(404, "slot not found")

    now = utcnow()
    if read_time(slot["start_at"]) <= now:
        raise ApiError(400, "slot has already started")

    booking = {
        "id": uuid.uuid4().hex,
        "slot_id": slot_id,
        "name": name,
        "email": email,
        "status": "confirmed",
        "created_at": store_time(now),
    }
    try:
        with database.connect() as conn, conn.tx():
            conn.execute(
                "INSERT INTO bookings (id, slot_id, name, email, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    booking["id"],
                    booking["slot_id"],
                    booking["name"],
                    booking["email"],
                    booking["status"],
                    booking["created_at"],
                ),
            )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise ApiError(409, "slot already booked") from None
        raise

    return 201, booking_json(booking), {}


def create_booking(
    payload: dict[str, Any], idempotency_key: str | None = None
) -> tuple[int, dict[str, Any], dict[str, str]]:
    slot_id = _text(payload.get("slot_id")).strip()
    name = _text(payload.get("name")).strip()
    email = _text(payload.get("email")).strip().lower()
    key = (idempotency_key or "").strip() or None

    if key:
        replay = _idempotency_claim(key, _body_hash(payload))
        if replay is not None:
            return replay

    try:
        if email:
            # Failed attempts count too, so the limit is checked before validation.
            _check_rate_limit(email)
        status, body, headers = _perform_booking(slot_id, name, email)
    except ApiError as exc:
        if key:
            if exc.status == 429:
                # Do not pin a rate-limited attempt to the key: after waiting,
                # the same key may still succeed.
                _idempotency_release(key)
            else:
                _idempotency_store(key, exc.status, {"error": exc.message})
        raise
    except BaseException:
        if key:
            _idempotency_release(key)
        raise

    if key:
        _idempotency_store(key, status, body)
    return status, body, headers


# ---------------------------------------------------------------------------
# Level 4 - listing, Level 1/3 - lookup & cancel
# ---------------------------------------------------------------------------
def list_bookings(email: Any, limit: Any = None, cursor: Any = None) -> dict[str, Any]:
    address = _text(email).strip().lower()
    if not address:
        raise ApiError(400, "email is required")

    page_size = 10
    if limit is not None and str(limit).strip() != "":
        try:
            page_size = int(str(limit).strip())
        except ValueError:
            raise ApiError(400, "limit must be an integer between 1 and 50") from None
        if page_size < 1 or page_size > 50:
            raise ApiError(400, "limit must be between 1 and 50")

    position = decode_cursor(cursor)

    sql = (
        "SELECT id, slot_id, name, email, status, created_at FROM bookings "
        "WHERE email = ?"
    )
    params: list[Any] = [address]
    if position is not None:
        sql += " AND (created_at > ? OR (created_at = ? AND id > ?))"
        params.extend([position[0], position[0], position[1]])
    sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
    params.append(page_size + 1)

    with database.connect() as conn:
        rows = conn.execute(sql, params).all()

    has_more = len(rows) > page_size
    rows = rows[:page_size]
    next_cursor = (
        encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more and rows else None
    )
    return {"bookings": [booking_json(row) for row in rows], "next_cursor": next_cursor}


def get_booking(booking_id: str) -> dict[str, Any]:
    booking_id = _text(booking_id).strip()
    with database.connect() as conn:
        row = conn.execute(
            "SELECT id, slot_id, name, email, status, created_at FROM bookings WHERE id = ?",
            (booking_id,),
        ).first()
    if row is None:
        raise ApiError(404, "booking not found")
    return booking_json(row)


def cancel_booking(booking_id: str) -> dict[str, Any]:
    booking_id = _text(booking_id).strip()
    with database.connect() as conn, conn.tx():
        result = conn.execute(
            "UPDATE bookings SET status = 'cancelled' "
            "WHERE id = ? AND status = 'confirmed'",
            (booking_id,),
        )
        if result.rowcount == 0:
            row = conn.execute(
                "SELECT status FROM bookings WHERE id = ?", (booking_id,)
            ).first()
            if row is None:
                raise ApiError(404, "booking not found")
            raise ApiError(409, "booking is already cancelled")
        row = conn.execute(
            "SELECT id, slot_id, name, email, status, created_at FROM bookings WHERE id = ?",
            (booking_id,),
        ).first()
    return booking_json(row)
