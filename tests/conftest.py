"""Boots a real uvicorn server against a throwaway SQLite database.

Using a real HTTP server (rather than TestClient) means the concurrency test
actually exercises parallel requests, exactly like production would.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
API_KEY = "test-api-key"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_server(tmp_path: Path, **overrides: str) -> Iterator[str]:
    port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "API_KEY": API_KEY,
            "SQLITE_PATH": str(tmp_path / "test.db"),
            "DATABASE_URL": "",
            "RATE_LIMIT_MAX": "5",
            "RATE_LIMIT_WINDOW_SECONDS": "60",
            "PYTHONPATH": str(ROOT),
        }
    )
    env.update(overrides)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
                raise RuntimeError(f"server exited early:\n{output}")
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not start in time")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


@pytest.fixture(scope="session")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with running_server(
        tmp_path_factory.mktemp("default"),
        DATABASE_URL=os.environ.get("TEST_DATABASE_URL", ""),
    ) as url:
        yield url


@pytest.fixture(scope="session")
def unlimited_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Same app with the level 5 limiter effectively off, for pagination tests."""
    database_url = os.environ.get("TEST_DATABASE_URL_UNLIMITED") or os.environ.get(
        "TEST_DATABASE_URL", ""
    )
    with running_server(
        tmp_path_factory.mktemp("nolimit"),
        RATE_LIMIT_MAX="1000",
        DATABASE_URL=database_url,
    ) as url:
        yield url


@pytest.fixture()
def api(base_url: str) -> httpx.Client:
    with httpx.Client(
        base_url=base_url,
        headers={"X-API-Key": API_KEY},
        timeout=30,
    ) as client:
        yield client


@pytest.fixture()
def unlimited_api(unlimited_base_url: str) -> httpx.Client:
    with httpx.Client(
        base_url=unlimited_base_url,
        headers={"X-API-Key": API_KEY},
        timeout=30,
    ) as client:
        yield client


def future_slot(index: int, *, provider: str = "Dr Ade") -> dict:
    """A slot that starts well after today, deterministic and sortable."""
    day = 10 + index // 12
    hour = 8 + index % 12
    return {
        "id": f"slot-{index}",
        "provider": provider,
        "start": f"2030-01-{day:02d}T{hour:02d}:00:00Z",
        "end": f"2030-01-{day:02d}T{hour:02d}:30:00Z",
    }


def reset(client: httpx.Client, slots: list[dict]) -> None:
    response = client.post("/reset", json={"slots": slots})
    assert response.status_code == 200, response.text
