# Clinic booking API

A small but complete booking API for a clinic, built for **Koya Cohort 4 — Task 1**.
It implements all five levels: listing and booking, API-key auth and validation,
race-free booking and cancellation, idempotent retries with cursor pagination, and
per-email rate limiting. A one-page web UI is served at `/`.

```
Python 3.13+ · FastAPI · PostgreSQL (Neon) in production · SQLite for local runs
```

## Endpoints

Every request needs the `X-API-Key` header. Every error is `{"error": "<message>"}`.

| Method | Path | Level | Notes |
|---|---|---|---|
| `POST` | `/reset` | 1 | Delete all bookings, replace all slots (past slots allowed) |
| `GET` | `/slots?provider=` | 1 | Earliest `start` first, each with `available` |
| `POST` | `/bookings` | 1–5 | `201`; `400` invalid/started; `404` unknown slot; `409` taken; `422` key reuse; `429` rate limited |
| `GET` | `/bookings?email=&limit=&cursor=` | 4 | Oldest first, cursor pagination, default limit 10 (1–50) |
| `GET` | `/bookings/{id}` | 1 | The booking |
| `DELETE` | `/bookings/{id}` | 3 | Cancel; frees the slot |
| `GET` | `/health` | — | Public liveness probe |
| `GET` | `/` | — | The web page |

`POST /bookings` also accepts an optional `Idempotency-Key` header: the same key
with the same body replays the original response (same status, same booking id);
the same key with a different body returns `422`.

## Run locally

No database server needed — SQLite is used when `DATABASE_URL` is empty.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:API_KEY = "local-dev-key"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000/> for the web page. It asks for the API key on first
load and keeps it in the browser's `localStorage` — the key is never served in the
page source, so opening the page does not expose the API.

```powershell
# seed a couple of slots
$body = @{
  slots = @(
    @{ id = "s1"; provider = "Dr Ade"; start = "2026-12-10T09:00:00Z"; end = "2026-12-10T09:30:00Z" }
  )
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/reset `
  -Headers @{ "X-API-Key" = "local-dev-key" } -ContentType "application/json" -Body $body

Invoke-RestMethod -Uri http://127.0.0.1:8000/slots -Headers @{ "X-API-Key" = "local-dev-key" }
```

To run against PostgreSQL locally, set `DATABASE_URL` (e.g. a Neon branch) — the
schema is created automatically on startup.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

The suite boots a real uvicorn process and covers auth, validation, cancellation,
pagination, idempotency, rate limiting, and — importantly — two concurrency tests
with a thread barrier: 30 simultaneous bookings for one slot must produce exactly
one `201`, and 10 simultaneous retries with one idempotency key must produce one
booking.

Set `TEST_DATABASE_URL` (and optionally `TEST_DATABASE_URL_UNLIMITED`) to run the
same suite against PostgreSQL rather than SQLite.

To check a deployed instance:

```powershell
$env:LIVE_URL = "https://your-service.onrender.com"
$env:LIVE_API_KEY = "your-key"
.\.venv\Scripts\python.exe -m pytest tests/test_live.py -q
```

`tests/test_live.py` calls `/reset`, so run it before handing the URL over.

## Deploy (Render + Neon)

1. **Neon** — create a free project, copy the *pooled* connection string
   (`postgresql://...-pooler...?sslmode=require`). The schema builds itself.
2. **Render** — New → Blueprint, point it at this repo (it reads `render.yaml`),
   or create a Web Service manually:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`
   - Environment: `API_KEY` (your key), `DATABASE_URL` (from step 1)
3. **Keep it warm** — free Render instances sleep after ~15 minutes idle and take
   ~50 s to wake. The workflow in `.github/workflows/keepalive.yml` pings
   `/health` every 5 minutes; set the repository variable `SERVICE_URL`
   (Settings → Secrets and variables → Actions → Variables) to your deployed URL,
   or edit the fallback value in the workflow. Any uptime monitor works too.

## How the hard parts work

**One booking per slot, even in a race (level 3).** The database enforces it:

```sql
CREATE UNIQUE INDEX bookings_one_confirmed_per_slot
    ON bookings (slot_id) WHERE status = 'confirmed';
```

The insert is attempted and a unique violation becomes `409`. Application-level
"check then insert" would have a race window; this has none, and it scales to any
number of workers. Cancelling flips `status` to `cancelled`, which drops the row
out of the partial index and frees the slot.

**Idempotent retries (level 4).** `idempotency_keys(key, body_hash, status, response)`
is claimed with `INSERT ... ON CONFLICT DO NOTHING` *before* any work happens, so a
concurrent duplicate waits for the winner and returns its stored response. A
different body hash under the same key is `422`. Failed responses are stored too;
`429`s are not, so retrying after the wait still works.

**Rate limiting (level 5).** A `booking_attempts` table holds one row per attempt;
the check is a sliding 60 s window counted *before* validation, so failed attempts
count. On Postgres the per-email check takes `pg_advisory_xact_lock`, so parallel
attempts can't both slip under the limit. `Retry-After` is the whole number of
seconds until the oldest attempt leaves the window.

**Pagination (level 4).** Keyset pagination on `(created_at, id)` encoded as an
opaque base64url cursor — stable even when timestamps collide, and it never skips
or repeats a row as new bookings arrive.

**Timing.** Slot `start`/`end` are echoed back exactly as supplied, while a
normalised UTC copy is stored for ordering and "has the slot started?" checks.

## Layout

```
app/main.py             routes, auth middleware, error handling, static page
app/logic.py            validation, booking, cancellation, idempotency, rate limit, cursors
app/db.py               SQLite/PostgreSQL wrapper, schema, connection pool
app/static/index.html   the web page
tests/                  end-to-end suite (real server, real HTTP)
render.yaml             Render blueprint
```
