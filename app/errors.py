"""Shared error type: every failure is {"error": "<message>"} with a status code."""

from __future__ import annotations

from typing import Mapping


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = dict(headers or {})
