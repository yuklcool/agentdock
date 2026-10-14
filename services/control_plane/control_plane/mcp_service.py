"""Pure helpers for the tenant MCP-server library (mirrors skills_service).

The auth secret follows the llm_credential rules: AES-GCM ciphertext at rest,
decrypted in memory only, never in any API response, never logged.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from agentcore.drivers.skills_md import MAX_DESCRIPTION, MAX_NAME, valid_skill_name
from agentcore.models import ShimMcpServer
from control_plane.auth.crypto import decrypt_secret, encrypt_secret
from control_plane.errors import api_error
from control_plane.ids import new_mcp_id

log = logging.getLogger(__name__)

AUTH_TYPES = {"none", "bearer", "header"}


def mcp_origin(value: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(value)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def validate_mcp_secret(secret: Any) -> None:
    if not isinstance(secret, str) or re.search(r"[\x00-\x1f\x7f]", secret):
        raise api_error(
            400, "validation_error", "secret must be text without control characters", "secret"
        )


def can_reuse_mcp_secret(existing: dict[str, Any], fields: dict[str, Any]) -> bool:
    try:
        return (
            mcp_origin(fields["url"]) == mcp_origin(existing["url"])
            and fields["auth_type"] == existing.get("auth_type", "none")
            and (
                fields["auth_type"] != "header"
                or fields.get("auth_header_name") == existing.get("auth_header_name")
            )
        )
    except ValueError:
        return False


def validate_mcp_connection(
    *,
    url: str,
    auth_type: str,
    auth_header_name: str | None,
    has_secret: bool,
) -> None:
    """The same connection validation for save and test; HTTP and HTTPS are peers."""
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and "\\" not in url
            and not re.search(r"[\s\x00-\x1f\x7f]", url)
        )
        _ = parsed.port  # validates numeric port and its range
    except ValueError:
        valid = False
    if not valid:
        raise api_error(400, "validation_error", "url must be a valid HTTP(S) endpoint", "url")
    if auth_type not in AUTH_TYPES:
        raise api_error(400, "validation_error", "unsupported authentication type", "auth_type")
    if auth_type == "header" and not re.fullmatch(
        r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", auth_header_name or ""
    ):
        raise api_error(
            400, "validation_error", "a valid header name is required", "auth_header_name"
        )
    if auth_type == "header" and (auth_header_name or "").lower() in {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "proxy-authorization",
        "mcp-session-id",
        "mcp-protocol-version",
        "accept",
        "content-type",
    }:
        raise api_error(400, "validation_error", "reserved header name", "auth_header_name")
    if auth_type != "none" and not has_secret:
        raise api_error(400, "validation_error", "secret is required for authentication", "secret")


def validate_mcp_fields(
    *,
    name: str,
    description: str,
    url: str,
    auth_type: str,
    auth_header_name: str | None,
    has_secret: bool,
) -> None:
    """Raise APIError(400) on the first invalid field."""
    if not valid_skill_name(name):
        raise api_error(
            400,
            "validation_error",
            f"name must match ^[a-z0-9]+(-[a-z0-9]+)*$ and be 1-{MAX_NAME} chars",
            "name",
        )
    if not (1 <= len(description) <= MAX_DESCRIPTION):
        raise api_error(
            400,
            "validation_error",
            f"description must be 1-{MAX_DESCRIPTION} chars",
            "description",
        )
    validate_mcp_connection(
        url=url,
        auth_type=auth_type,
        auth_header_name=auth_header_name,
        has_secret=has_secret,
    )


def build_mcp_row(
    *,
    tenant_id: str,
    created_by: str | None,
    name: str,
    description: str,
    url: str,
    auth_type: str,
    auth_header_name: str | None,
    secret: str,
    enabled: bool,
    master_key: bytes | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": new_mcp_id(),
        "tenant_id": tenant_id,
        "name": name,
        "description": description,
        "url": url,
        "auth_type": auth_type,
        "auth_header_name": auth_header_name or None,
        "secret_ciphertext": encrypt_secret(secret, master_key) if secret else None,
        "enabled": enabled,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
    }


def mcp_public_view(row: dict[str, Any]) -> dict[str, Any]:
    """List/detail view — tenant_id and the secret are never exposed. A boolean
    ``secret_set`` lets the console show 'secret configured' without the value."""
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "url": row["url"],
        "auth_type": row.get("auth_type", "none"),
        "auth_header_name": row.get("auth_header_name"),
        "secret_set": row.get("secret_ciphertext") is not None,
        "enabled": row["enabled"],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


def mcp_detail_view(row: dict[str, Any]) -> dict[str, Any]:
    """Single-server view. Identical to the public view — the secret is never
    returned, on any path."""
    return mcp_public_view(row)


def filter_known_mcp_server_ids(selected_ids: list[str], rows: list[dict[str, Any]]) -> list[str]:
    """Drop ids that don't belong to the tenant, preserving selection order."""
    known = {r["id"] for r in rows}
    return [sid for sid in selected_ids if sid in known]


def resolve_mcp_for_request(
    selected_ids: list[str],
    rows: list[dict[str, Any]],
    master_key: bytes,
) -> list[ShimMcpServer]:
    """Map selected ids -> ShimMcpServer, preserving selection order, keeping only
    enabled rows. Secrets are decrypted in memory. A row whose secret fails to
    decrypt is dropped and logged (never silently)."""
    by_id = {r["id"]: r for r in rows if r.get("enabled")}
    out: list[ShimMcpServer] = []
    for mid in selected_ids:
        r = by_id.get(mid)
        if r is None:
            continue
        secret = ""
        ct = r.get("secret_ciphertext")
        if ct is not None:
            try:
                secret = decrypt_secret(bytes(ct), master_key)
            except Exception:  # noqa: BLE001 — a bad key/ciphertext must not crash the task
                log.warning("mcp server %s dropped: secret decrypt failed", r.get("name"))
                continue
        out.append(
            ShimMcpServer(
                name=r["name"],
                url=r["url"],
                auth_type=r.get("auth_type", "none"),
                auth_header_name=r.get("auth_header_name") or "",
                secret=secret,
            )
        )
    return out
