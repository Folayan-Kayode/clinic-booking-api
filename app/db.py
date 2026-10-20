"""Database access.

One small wrapper over SQLite (local development / tests) and PostgreSQL
(production, Neon).  All timestamps are stored as fixed-width UTC ISO-8601
text so that both engines sort and compare them identically.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

log = logging.getLogger(__name__)

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
SQLITE_PATH = (os.environ.get("SQLITE_PATH") or os.path.join("data", "app.db")).strip()

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS slots (
        id         TEXT PRIMARY KEY,
        provider   TEXT NOT NULL,
        start_at   TEXT NOT NULL,
        start_raw  TEXT NOT NULL,
        end_at     TEXT NOT NULL,
        end_raw    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bookings (
        id         TEXT PRIMARY KEY,
        slot_id    TEXT NOT NULL,
        name       TEXT NOT NULL,
        email      TEXT NOT NULL,
        status     TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # The level 3 invariant, enforced by the database rather than by the app:
    # at most one *confirmed* booking per slot, no matter how many requests
    # arrive at the same instant.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS bookings_one_confirmed_per_slot
        ON bookings (slot_id) WHERE status = 'confirmed'
    """,
    """
    CREATE INDEX IF NOT EXISTS bookings_email_created
        ON bookings (email, created_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key        TEXT PRIMARY KEY,
        body_hash  TEXT NOT NULL,
        status     INTEGER,
        response   TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS booking_attempts (
        email        TEXT NOT NULL,
        attempted_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS booking_attempts_email_time
        ON booking_attempts (email, attempted_at)
    """,
)


class Result:
    """Thin cursor wrapper returning plain dicts."""

    def __init__(self, cursor: Any, dialect: str) -> None:
        self._cursor = cursor
        self._dialect = dialect

    def first(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return dict(row) if self._dialect == "sqlite" else row

    def all(self) -> list[dict[str, Any]]:
        rows = self._cursor.fetchall()
        if self._dialect == "sqlite":
            return [dict(row) for row in rows]
        return rows

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class Connection:
    def __init__(self, raw: Any, dialect: str) -> None:
        self._raw = raw
        self.dialect = dialect

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        cursor = self._raw.cursor()
        if self.dialect == "postgres":
            cursor.execute(sql.replace("?", "%s"), tuple(params))
        else:
            cursor.execute(sql, tuple(params))
        return Result(cursor, self.dialect)

    @contextmanager
    def tx(self) -> Iterator["Connection"]:
        """Atomic block.  Postgres connections are in autocommit mode, SQLite
        ones use explicit BEGIN IMMEDIATE so writers queue instead of failing."""
        if self.dialect == "sqlite":
            self._raw.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self._raw.execute("COMMIT")
            except BaseException:
                self._raw.execute("ROLLBACK")
                raise
        else:
            with self._raw.transaction():
                yield self


class Database:
    def __init__(self) -> None:
        self.dialect = (
            "postgres"
            if DATABASE_URL.startswith(("postgres://", "postgresql://"))
            else "sqlite"
        )
        self._pool = None
        self._local = threading.local()

    # -- connections ----------------------------------------------------
    def _sqlite_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if SQLITE_PATH != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(SQLITE_PATH)) or ".", exist_ok=True)
            conn = sqlite3.connect(
                SQLITE_PATH, timeout=15, isolation_level=None, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        if self.dialect == "postgres":
            assert self._pool is not None
            with self._pool.connection() as raw:
                yield Connection(raw, "postgres")
        else:
            yield Connection(self._sqlite_connection(), "sqlite")

    # -- lifecycle ------------------------------------------------------
    def open(self) -> None:
        if self.dialect == "postgres":
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            # prepare_threshold=None keeps us compatible with PgBouncer /
            # Neon's pooled endpoint; check_connection drops dead links that
            # the database closed while we were idle.
            self._pool = ConnectionPool(
                conninfo=DATABASE_URL,
                min_size=0,
                max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
                timeout=30,
                max_idle=120,
                check=ConnectionPool.check_connection,
                kwargs={
                    "autocommit": True,
                    "row_factory": dict_row,
                    "prepare_threshold": None,
                },
            )
            self._pool.open(wait=True, timeout=30)
        else:
            self._sqlite_connection()

        self._create_schema()
        log.info("database ready (%s)", self.dialect)

    def _create_schema(self) -> None:
        with self.connect() as conn:
            for statement in SCHEMA_STATEMENTS:
                try:
                    conn.execute(statement)
                except Exception as exc:  # index already created by a racing instance
                    if "already exists" in str(exc).lower():
                        continue
                    raise
            # Fail fast if anything is missing.
            for table in ("slots", "bookings", "idempotency_keys", "booking_attempts"):
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1").first()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


database = Database()
