"""Nanobot template overrides. Defaults belong exclusively to native Nanobot."""

from __future__ import annotations

import json
from typing import Any

NANOBOT_FILES = ("config.json", "AGENTS.md", "SOUL.md", "USER.md", "HEARTBEAT.md", "TOOLS.md")
MAX_FILE_BYTES = 64 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("config.json cannot contain duplicate keys")
        result[key] = value
    return result


def parse_native_config(content: str) -> dict[str, Any]:
    def invalid_constant(value: str) -> None:
        raise ValueError("config.json must use finite JSON values")

    try:
        result = json.loads(content, object_pairs_hook=_unique_object,
                            parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("config.json must be a valid JSON object without duplicate keys") from exc
    if not isinstance(result, dict):
        raise ValueError("config.json must be a JSON object")
    # Model credentials are resolved by the control plane, never stored in a
    # template/snapshot. Empty native defaults are accepted.
    def check(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.replace("_", "").lower() in {
                    "apikey", "accesstoken", "refreshtoken", "authorization",
                } and item:
                    raise ValueError("Store credentials in Credentials, not config.json")
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    try:
        check(result)
    except RecursionError as exc:
        raise ValueError("config.json nesting is too deep") from exc
    return result


def validate_nanobot_files(raw: Any, *, driver: str = "nanobot") -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - set(NANOBOT_FILES):
        raise ValueError("nanobot_initial_files must contain only the six supported filenames")
    result: dict[str, str] = {}
    for name, value in raw.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("Initialization file overrides must be text or null")
        try:
            if len(value.encode("utf-8")) > MAX_FILE_BYTES:
                raise ValueError("Each initialization file must be at most 64 KiB")
        except UnicodeEncodeError as exc:
            raise ValueError("Initialization files must contain valid UTF-8 text") from exc
        if name == "config.json":
            parse_native_config(value)
        result[name] = value
    if driver != "nanobot" and result:
        raise ValueError("Initialization files are available only for Nanobot templates")
    return result


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        result[key] = (
            merge_config(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict) else value
        )
    return result
