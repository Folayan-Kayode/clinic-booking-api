"""End-to-end checks for every level of the booking API."""

from __future__ import annotations

import threading
import uuid

import httpx
import pytest

from conftest import API_KEY, future_slot, reset

PAST_SLOT = {
    "id": "past-1",
    "provider": "Dr Ade",
    "start": "2020-01-01T09:00:00Z",
    "end": "2020-01-01T09:30:00Z",
}


# ---------------------------------------------------------------------------
# level 2 - authentication
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/slots"),
        ("GET", "/bookings?email=a@b.com"),
        ("GET", "/bookings/whatever"),
        ("DELETE", "/bookings/whatever"),
        ("POST", "/reset"),
        ("POST", "/bookings"),
    ],
)
def test_missing_or_wrong_key_is_401(base_url: str, method: str, path: str) -> None:
    for headers in ({}, {"X-API-Key": "nope"}):
        response = httpx.request(
            method, base_url + path, headers=headers, json={"slots": []}
        )
        assert response.status_code == 401, (method, path, response.text)
        assert set(response.json()) == {"error"}


def test_health_is_public(base_url: str) -> None:
    response = httpx.get(base_url + "/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# level 1 - reset, list, book, get
# ---------------------------------------------------------------------------
def test_reset_replaces_slots_and_sorts_by_start(api: httpx.Client) -> None:
    reset(
        api,
        [
            {
                "id": "s2",
                "provider": "Dr Bello",
                "start": "2026-12-10T10:00:00+01:00",  # 09:00Z
                "end": "2026-12-10T10:30:00+01:00",
            },
            {
                "id": "s1",
                "provider": "Dr Ade",
                "start": "2026-12-10T11:00:00Z",
                "end": "2026-12-10T11:30:00Z",
            },
            PAST_SLOT,
        ],
    )

    slots = api.get("/slots").json()["slots"]
    assert [slot["id"] for slot in slots] == ["past-1", "s2", "s1"]
    assert slots[0]["start"] == "2020-01-01T09:00:00Z"
    assert all(slot["available"] is True for slot in slots)
    assert set(slots[0]) == {"id", "provider", "start", "end", "available"}

    only_ade = api.get("/slots", params={"provider": "Dr Ade"}).json()["slots"]
    assert {slot["id"] for slot in only_ade} == {"s1", "past-1"}
    assert api.get("/slots", params={"provider": "Dr ade"}).json()["slots"] == []


def test_book_lookup_and_cancel(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])

    created = api.post(
        "/bookings",
        json={"slot_id": "slot-0", "name": "  Ada Obi  ", "email": "  ADA@Example.COM "},
    )
    assert created.status_code == 201, created.text
    booking = created.json()
    assert booking["slot_id"] == "slot-0"
    assert booking["name"] == "Ada Obi"
    assert booking["email"] == "ada@example.com"
    assert booking["status"] == "confirmed"
    assert set(booking) == {"id", "slot_id", "name", "email", "status", "created_at"}
    assert booking["created_at"].endswith("Z")

    fetched = api.get(f"/bookings/{booking['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == booking

    slot = api.get("/slots").json()["slots"][0]
    assert slot["available"] is False

    cancelled = api.delete(f"/bookings/{booking['id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert api.get(f"/bookings/{booking['id']}").json()["status"] == "cancelled"
    assert api.get("/slots").json()["slots"][0]["available"] is True

    # The freed slot can be booked again.
    again = api.post(
        "/bookings",
        json={"slot_id": "slot-0", "name": "Bob", "email": "bob@example.com"},
    )
    assert again.status_code == 201, again.text

    assert api.delete(f"/bookings/{booking['id']}").status_code == 409
    assert api.delete("/bookings/does-not-exist").status_code == 404
    assert api.get("/bookings/does-not-exist").status_code == 404


def test_reset_deletes_bookings(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])
    booking = api.post(
        "/bookings",
        json={"slot_id": "slot-0", "name": "Ada", "email": "ada@example.com"},
    ).json()
    reset(api, [future_slot(0)])
    assert api.get(f"/bookings/{booking['id']}").status_code == 404


# ---------------------------------------------------------------------------
# level 2 - validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Ada", "email": "ada@example.com"},
        {"slot_id": "", "name": "Ada", "email": "ada@example.com"},
        {"slot_id": "slot-0", "email": "ada@example.com"},
        {"slot_id": "slot-0", "name": "   ", "email": "ada@example.com"},
        {"slot_id": "slot-0", "name": "Ada"},
        {"slot_id": "slot-0", "name": "Ada", "email": ""},
        {"slot_id": "slot-0", "name": "Ada", "email": "not-an-email"},
        {"slot_id": "slot-0", "name": "Ada", "email": "ada@example"},
        {"slot_id": "slot-0", "name": "Ada", "email": "ada @example.com"},
        {"slot_id": 42, "name": "Ada", "email": "ada@example.com"},
    ],
)
def test_invalid_payloads_are_400(api: httpx.Client, payload: dict) -> None:
    reset(api, [future_slot(0)])
    response = api.post("/bookings", json=payload)
    assert response.status_code == 400, response.text
    assert "error" in response.json()


def test_unknown_slot_is_404(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])
    response = api.post(
        "/bookings",
        json={"slot_id": "ghost", "name": "Ada", "email": "ada@example.com"},
    )
    assert response.status_code == 404
    assert response.json() == {"error": "slot not found"}


def test_started_slot_is_400(api: httpx.Client) -> None:
    reset(api, [PAST_SLOT])
    response = api.post(
        "/bookings",
        json={"slot_id": "past-1", "name": "Ada", "email": "ada@example.com"},
    )
    assert response.status_code == 400


def test_malformed_json_is_400(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])
    response = api.post(
        "/bookings",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# level 3 - concurrency
# ---------------------------------------------------------------------------
def test_only_one_of_many_simultaneous_bookings_wins(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])
    attempts = 30
    barrier = threading.Barrier(attempts)
    results: list[int] = []
    lock = threading.Lock()

    def book(index: int) -> None:
        with httpx.Client(
            base_url=str(api.base_url), headers={"X-API-Key": API_KEY}, timeout=60
        ) as client:
            barrier.wait()
            response = client.post(
                "/bookings",
                json={
                    "slot_id": "slot-0",
                    "name": f"Patient {index}",
                    "email": f"patient{index}@example.com",
                },
            )
            with lock:
                results.append(response.status_code)

    threads = [threading.Thread(target=book, args=(i,)) for i in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(201) == 1, results
    assert results.count(409) == attempts - 1, results
    assert len(api.get("/bookings", params={"email": "patient5@example.com"}).json()["bookings"]) <= 1


# ---------------------------------------------------------------------------
# level 4 - idempotency
# ---------------------------------------------------------------------------
def test_idempotent_replay_returns_same_booking(api: httpx.Client) -> None:
    reset(api, [future_slot(0), future_slot(1)])
    headers = {"Idempotency-Key": f"key-{uuid.uuid4()}"}
    body = {"slot_id": "slot-0", "name": "Ada", "email": "ada@example.com"}

    first = api.post("/bookings", json=body, headers=headers)
    second = api.post("/bookings", json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()

    # A different body under the same key is rejected.
    clash = api.post(
        "/bookings",
        json={"slot_id": "slot-1", "name": "Ada", "email": "ada@example.com"},
        headers=headers,
    )
    assert clash.status_code == 422
    assert "error" in clash.json()

    # Still exactly one booking for the person.
    bookings = api.get("/bookings", params={"email": "ada@example.com"}).json()["bookings"]
    assert len(bookings) == 1


def test_idempotent_replay_of_a_failure(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])
    headers = {"Idempotency-Key": f"key-{uuid.uuid4()}"}
    body = {"slot_id": "ghost", "name": "Ada", "email": "ada@example.com"}
    first = api.post("/bookings", json=body, headers=headers)
    second = api.post("/bookings", json=body, headers=headers)
    assert first.status_code == second.status_code == 404
    assert first.json() == second.json()


def test_concurrent_same_key_creates_one_booking(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])
    headers = {"Idempotency-Key": f"key-{uuid.uuid4()}"}
    body = {"slot_id": "slot-0", "name": "Ada", "email": "ada@example.com"}
    barrier = threading.Barrier(10)
    responses: list[httpx.Response] = []
    lock = threading.Lock()

    def send() -> None:
        with httpx.Client(
            base_url=str(api.base_url),
            headers={"X-API-Key": API_KEY, **headers},
            timeout=60,
        ) as client:
            barrier.wait()
            response = client.post("/bookings", json=body)
            with lock:
                responses.append(response)

    threads = [threading.Thread(target=send) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert {response.status_code for response in responses} == {201}, [
        response.status_code for response in responses
    ]
    assert len({response.json()["id"] for response in responses}) == 1

    bookings = api.get("/bookings", params={"email": "ada@example.com"}).json()["bookings"]
    assert len(bookings) == 1


# ---------------------------------------------------------------------------
# level 4 - pagination
# ---------------------------------------------------------------------------
def test_cursor_pagination(unlimited_api: httpx.Client) -> None:
    slots = [future_slot(index) for index in range(12)]
    reset(unlimited_api, slots)

    ids: list[str] = []
    for index in range(12):
        response = unlimited_api.post(
            "/bookings",
            json={
                "slot_id": f"slot-{index}",
                "name": f"Patient {index}",
                "email": "Pager@Example.com",
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])

    collected: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"email": "pager@example.com", "limit": 5}
        if cursor:
            params["cursor"] = cursor
        page = unlimited_api.get("/bookings", params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        assert set(body) == {"bookings", "next_cursor"}
        collected.extend(booking["id"] for booking in body["bookings"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert collected == ids  # oldest first, no duplicates, no gaps

    default_page = unlimited_api.get("/bookings", params={"email": "pager@example.com"}).json()
    assert len(default_page["bookings"]) == 10
    assert default_page["next_cursor"]

    biggest = unlimited_api.get(
        "/bookings", params={"email": "pager@example.com", "limit": 50}
    ).json()
    assert len(biggest["bookings"]) == 12
    assert biggest["next_cursor"] is None


@pytest.mark.parametrize("limit", ["0", "51", "-1", "abc", "2.5"])
def test_invalid_limit_is_400(unlimited_api: httpx.Client, limit: str) -> None:
    reset(unlimited_api, [future_slot(0)])
    response = unlimited_api.get(
        "/bookings", params={"email": "pager@example.com", "limit": limit}
    )
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("params", [{}, {"email": ""}, {"email": "   "}])
def test_list_requires_email(unlimited_api: httpx.Client, params: dict) -> None:
    reset(unlimited_api, [future_slot(0)])
    assert unlimited_api.get("/bookings", params=params).status_code == 400


def test_invalid_cursor_is_400(unlimited_api: httpx.Client) -> None:
    reset(unlimited_api, [future_slot(0)])
    response = unlimited_api.get(
        "/bookings", params={"email": "pager@example.com", "cursor": "not-a-cursor"}
    )
    assert response.status_code == 400


def test_listing_is_case_insensitive_and_oldest_first(api: httpx.Client) -> None:
    reset(api, [future_slot(index) for index in range(3)])
    for index in range(3):
        api.post(
            "/bookings",
            json={
                "slot_id": f"slot-{index}",
                "name": "Ada",
                "email": "ADA@example.com",
            },
        )
    bookings = api.get("/bookings", params={"email": "ada@EXAMPLE.com"}).json()["bookings"]
    assert [booking["slot_id"] for booking in bookings] == ["slot-0", "slot-1", "slot-2"]


# ---------------------------------------------------------------------------
# level 5 - rate limit
# ---------------------------------------------------------------------------
def test_rate_limit_is_five_per_email_per_minute(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])
    email = "spam@example.com"
    payload = {"slot_id": "ghost", "name": "Spam", "email": email}

    for _ in range(5):
        assert api.post("/bookings", json=payload).status_code == 404

    blocked = api.post("/bookings", json=payload)
    assert blocked.status_code == 429, blocked.text
    assert "error" in blocked.json()
    retry_after = blocked.headers.get("Retry-After")
    assert retry_after is not None and retry_after.isdigit()
    assert 1 <= int(retry_after) <= 60

    # A different email is unaffected.
    other = api.post(
        "/bookings",
        json={"slot_id": "slot-0", "name": "Ada", "email": "ada@example.com"},
    )
    assert other.status_code == 201

    # Reset clears the limiter (so a test run can start from scratch).
    reset(api, [future_slot(0)])
    assert api.post("/bookings", json={**payload, "email": email}).status_code == 404


def test_failed_attempts_count_towards_the_limit(api: httpx.Client) -> None:
    reset(api, [future_slot(0)])
    email = "typo@example.com"
    outcomes = []
    for index in range(5):
        outcomes.append(
            api.post(
                "/bookings",
                json={
                    "slot_id": f"missing-{index}",
                    "name": "Typo",
                    "email": email,
                },
            ).status_code
        )
    assert outcomes == [404] * 5
    assert api.post(
        "/bookings",
        json={"slot_id": "slot-0", "name": "Typo", "email": email},
    ).status_code == 429


# ---------------------------------------------------------------------------
# web page
# ---------------------------------------------------------------------------
def test_web_page_is_served(base_url: str) -> None:
    response = httpx.get(base_url + "/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Clinic appointments" in response.text
    # Level 2 must not be bypassable by reading the page source.
    assert API_KEY not in response.text
    assert "__API_KEY_JSON__" not in response.text
