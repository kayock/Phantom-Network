"""Shared domain error type (kept in its own module to avoid import cycles)."""

from __future__ import annotations


class FsError(Exception):
    """Domain error carrying an HTTP status and a user-facing message.

    Raised by filesystem, transfer, chat and audio services; converted to a
    clean JSON error response by the HTTP dispatcher.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
