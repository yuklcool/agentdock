"""Shared validation for model API base URLs (never carries credentials)."""

from __future__ import annotations

from typing import Any

from pydantic import HttpUrl, TypeAdapter


def normalize_base_url(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Base URL must be an HTTP(S) URL")
    value = value.strip()
    if not value:
        return None
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Base URL cannot contain whitespace or backslashes")
    url = TypeAdapter(HttpUrl).validate_python(value)
    if url.username is not None or url.password is not None:
        raise ValueError("Keep credentials in the API key field, not the Base URL")
    if url.query is not None or url.fragment is not None:
        raise ValueError("Base URL cannot contain query parameters or a fragment")
    return str(url).rstrip("/")


def api_key_base_url(kind: str, metadata: dict[str, Any] | None) -> str | None:
    """Subscription tokens must never be redirected to a tenant API-key endpoint."""
    if kind != "api_key":
        return None
    return normalize_base_url((metadata or {}).get("base_url"))
