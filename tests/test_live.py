"""Checks a deployed instance over the internet.

    $env:LIVE_URL = "https://your-service.onrender.com"
    $env:LIVE_API_KEY = "your-key"
    python -m pytest tests/test_live.py -q

This calls /reset, so run it before you hand the URL over.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

LIVE_URL = (os.environ.get("LIVE_URL") or "").rstrip("/")
LIVE_KEY = os.environ.get("LIVE_API_KEY") or ""

pytestmark = pytest.mark.skipif(
    not LIVE_URL or not LIVE_KEY, reason="set LIVE_URL and LIVE_API_KEY to run"
)


@pytest.fixture(scope="module")
def live() -> httpx.Client:
    with httpx.Client(
        base_url=LIVE_URL,
        headers={"X-API-Key": LIVE_KEY},
        timeout=120,  # free instances can cold start
    ) as client:
        yield client


def slots_payload() -> list[dict]:
    return [
        {
            "id": "live-1",
            "provider": "Dr Ade",
            "start": "2031-03-01T09:00:00Z",
            "end": "2031-03-01T09:30:00Z",
        },
        {
            "id": "live-2",
            "provider": "Dr Bello",
            "start": "2031-03-01T10:00:00Z",
            "end": "2031-03-01T10:30:00Z",
        },
        {
            "id": "live-3",
            "provider": "Dr Ade",
            "start": "2031-03-02T09:00:00Z",
            "end": "2031-03-02T09:30:00Z",
        },
    ]


def test_auth_is_enforced(live: httpx.Client) -> None:
    assert httpx.get(LIVE_URL + "/health", timeout=120).status_code == 200
    assert httpx.get(LIVE_URL + "/slots", timeout=120).status_code == 401
    assert (
        httpx.get(
            LIVE_URL + "/slots", headers={"X-API-Key": "wrong"}, timeout=120
        ).status_code
        == 401
    )


def test_full_flow(live: httpx.Client) -> None:
    email = f"live-{uuid.uuid4().hex[:8]}@example.com"
    assert live.post("/reset", json={"slots": slots_payload()}).status_code == 200

    slots = live.get("/slots").json()["slots"]
    assert [slot["id"] for slot in slots] == ["live-1", "live-2", "live-3"]
    assert all(slot["available"] for slot in slots)

    created = live.post(
        "/bookings", json={"slot_id": "live-1", "name": " Live Tester ", "email": email.upper()}
    )
    assert created.status_code == 201, created.text
    booking = created.json()
    assert booking["name"] == "Live Tester"
    assert booking["email"] == email
    assert booking["status"] == "confirmed"

    # Level 3: the slot is taken, and only once.
    duplicate = live.post(
        "/bookings", json={"slot_id": "live-1", "name": "Other", "email": f"x-{email}"}
    )
    assert duplicate.status_code == 409

    # Level 4: a retried key returns the same booking.
    key = {"Idempotency-Key": f"live-{uuid.uuid4()}"}
    body = {"slot_id": "live-2", "name": "Live Tester", "email": email}
    first = live.post("/bookings", json=body, headers=key)
    replay = live.post("/bookings", json=body, headers=key)
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    assert first.json() == replay.json()

    # Level 4: pagination.
    third = live.post(
        "/bookings", json={"slot_id": "live-3", "name": "Live Tester", "email": email}
    )
    assert third.status_code == 201
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"email": email, "limit": 1}
        if cursor:
            params["cursor"] = cursor
        page = live.get("/bookings", params=params)
        assert page.status_code == 200, page.text
        payload = page.json()
        seen.extend(item["id"] for item in payload["bookings"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break
    assert seen == [booking["id"], first.json()["id"], third.json()["id"]]

    # Level 3: cancel and rebook.
    cancelled = live.delete(f"/bookings/{booking['id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert live.delete(f"/bookings/{booking['id']}").status_code == 409
    assert live.get("/slots").json()["slots"][0]["available"] is True

    # Level 5: the sixth attempt in a minute is refused (failed attempts count).
    spam = {"slot_id": "ghost", "name": "Spam", "email": f"spam-{email}"}
    for _ in range(5):
        assert live.post("/bookings", json=spam).status_code == 404
    blocked = live.post("/bookings", json=spam)
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After", "").isdigit()

    assert live.get(f"/bookings/{booking['id']}").json()["status"] == "cancelled"
    assert live.get(f"/bookings/{booking['id']}x").status_code == 404

    # Leave the service clean for the real checks.
    assert live.post("/reset", json={"slots": slots_payload()}).status_code == 200
