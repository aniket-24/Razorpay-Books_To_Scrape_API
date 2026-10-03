"""Errors that are safe to return to API callers."""

from typing import Dict, Optional


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


def invalid_query(message: str) -> ApiError:
    return ApiError(400, "invalid_query", message)


def layout_changed() -> ApiError:
    return ApiError(
        502,
        "source_layout_changed",
        "The source HTML no longer matches the expected catalogue structure.",
    )