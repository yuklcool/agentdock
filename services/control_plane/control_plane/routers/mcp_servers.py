"""Tenant MCP-server library CRUD. Reads are tenant-scoped; writes are
admin-gated. Mirrors routers/skills.py. The auth secret is encrypted at rest and
never returned (see mcp_service)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from agentcore.mcp_probe import probe_mcp_server
from agentcore.models import ShimMcpServer
from control_plane.auth.crypto import decrypt_secret, encrypt_secret, load_key_from_env
from control_plane.auth.principal import Principal, require_admin, resolve_principal
from control_plane.errors import api_error, not_found
from control_plane.mcp_service import (
    build_mcp_row,
    can_reuse_mcp_secret,
    mcp_detail_view,
    mcp_public_view,
    validate_mcp_connection,
    validate_mcp_fields,
    validate_mcp_secret,
)
from control_plane.models_db import mcp_servers
from control_plane.skills_service import normalize_description

router = APIRouter(tags=["MCP Servers"])


class MCPTestInput(BaseModel):
    """Optional overrides for an existing server; drafts require a URL."""

    url: str | None = Field(default=None, max_length=4096)
    auth_type: str | None = None
    auth_header_name: str | None = Field(default=None, max_length=200)
    secret: str | None = Field(default=None, max_length=16384)


async def _test_connection(
    request: Request,
    body: MCPTestInput,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    url = body.url if body.url is not None else existing.get("url", "")
    auth_type = body.auth_type if body.auth_type is not None else existing.get("auth_type", "none")
    header = (
        body.auth_header_name
        if "auth_header_name" in body.model_fields_set
        else existing.get("auth_header_name")
    )
    secret = body.secret
    if secret is None and auth_type != "none" and existing.get("secret_ciphertext") is not None:
        if not can_reuse_mcp_secret(
            existing,
            {
                "url": url,
                "auth_type": auth_type,
                "auth_header_name": header,
            },
        ):
            raise api_error(
                400, "validation_error", "enter a new secret for changed endpoint/auth", "secret"
            )
        try:
            secret = decrypt_secret(bytes(existing["secret_ciphertext"]), load_key_from_env())
        except Exception:
            raise api_error(
                400, "secret_unavailable", "stored secret could not be decrypted", "secret"
            ) from None
    secret = secret or ""
    validate_mcp_secret(secret)
    validate_mcp_connection(
        url=url,
        auth_type=auth_type,
        auth_header_name=header,
        has_secret=bool(secret),
    )
    server = ShimMcpServer(
        name=existing.get("name", "connection-test"),
        url=url,
        auth_type=auth_type,
        auth_header_name=header or "",
        secret=secret,
    )
    # Use the same filtering proxy as agents; never silently bypass it on failure.
    proxy_url = os.environ.get("AGENTDOCK_MCP_TEST_PROXY") or "http://egress-proxy:8888"
    if not hasattr(request.app.state, "mcp_test_limit"):
        request.app.state.mcp_test_limit = asyncio.Semaphore(4)
    limit = request.app.state.mcp_test_limit
    try:
        await asyncio.wait_for(limit.acquire(), timeout=0.1)
    except TimeoutError:
        raise api_error(429, "test_busy", "too many connection tests; try again shortly") from None
    try:
        return await probe_mcp_server(server, proxy_url=proxy_url)
    finally:
        limit.release()


@router.post("/mcp-servers/test", response_description="MCP handshake/tool discovery diagnostics.")
async def test_mcp_draft(
    body: MCPTestInput,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Test without saving. Admin-only, tenant-scoped, secrets never returned."""
    if principal.tenant_id is None:
        raise api_error(403, "forbidden", "MCP servers are tenant-scoped")
    return await _test_connection(request, body)


@router.post(
    "/mcp-servers/{mid}/test", response_description="Test saved/temporarily overridden MCP."
)
async def test_saved_mcp(
    mid: str,
    body: MCPTestInput,
    request: Request,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Load only the caller's server. Overrides affect this test, never the DB."""
    if principal.tenant_id is None:
        raise api_error(403, "forbidden", "MCP servers are tenant-scoped")
    async with request.app.state.session_factory() as session:
        result = await session.execute(
            select(*_ROW_COLS).where(
                mcp_servers.c.id == mid,
                mcp_servers.c.tenant_id == principal.tenant_id,
            )
        )
        row = result.fetchone()
    if row is None:
        raise not_found("mcp server not found")
    return await _test_connection(request, body, dict(row._mapping))


# ---- response models (documentation only) -----------------------------------


class MCPServerListResponse(BaseModel):
    """Envelope returned by ``GET /mcp-servers``."""

    mcp_servers: list[dict[str, Any]] = Field(
        description="The tenant's MCP servers; the auth secret is never exposed, "
        "only a boolean ``secret_set``. Sorted by name."
    )


# secret_ciphertext is included solely to derive secret_set; it is never returned.
_ROW_COLS = [
    mcp_servers.c.id,
    mcp_servers.c.tenant_id,
    mcp_servers.c.name,
    mcp_servers.c.description,
    mcp_servers.c.url,
    mcp_servers.c.auth_type,
    mcp_servers.c.auth_header_name,
    mcp_servers.c.secret_ciphertext,
    mcp_servers.c.enabled,
    mcp_servers.c.created_by,
    mcp_servers.c.created_at,
    mcp_servers.c.updated_at,
]


def parse_mcp_create(body: dict[str, Any]) -> dict[str, Any]:
    """Validate a create payload -> normalized field dict (incl. plaintext secret)."""
    name = body.get("name")
    description = body.get("description")
    url = body.get("url")
    if not isinstance(name, str):
        raise api_error(400, "validation_error", "name is required", "name")
    if not isinstance(description, str):
        raise api_error(400, "validation_error", "description is required", "description")
    if not isinstance(url, str):
        raise api_error(400, "validation_error", "url is required", "url")
    description = normalize_description(description)
    auth_type = body.get("auth_type", "none")
    auth_header_name = body.get("auth_header_name") or None
    secret = body.get("secret", "") or ""
    validate_mcp_secret(secret)
    validate_mcp_fields(
        name=name,
        description=description,
        url=url,
        auth_type=auth_type,
        auth_header_name=auth_header_name,
        has_secret=bool(secret),
    )
    return {
        "name": name,
        "description": description,
        "url": url,
        "auth_type": auth_type,
        "auth_header_name": auth_header_name,
        "secret": secret,
        "enabled": bool(body.get("enabled", True)),
    }


def apply_mcp_patch(existing: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Merge a PATCH onto existing fields and re-validate. Returns (merged, directive)
    where directive is 'keep' | 'set' | 'clear' for the secret column."""
    merged = {
        k: existing.get(k)
        for k in ("name", "description", "url", "auth_type", "auth_header_name", "enabled")
    }
    for k in ("name", "description", "url", "auth_type", "auth_header_name", "enabled"):
        if k in patch:
            merged[k] = patch[k]
    if "description" in patch:
        merged["description"] = normalize_description(str(merged["description"]))
    merged["auth_header_name"] = merged["auth_header_name"] or None
    merged["enabled"] = bool(merged["enabled"])

    directive = "keep"
    if "secret" in patch:
        validate_mcp_secret(patch["secret"])
        directive = "set" if patch["secret"] else "clear"
    if (
        directive == "keep"
        and existing.get("secret_ciphertext") is not None
        and merged["auth_type"] != "none"
        and not can_reuse_mcp_secret(existing, merged)
    ):
        raise api_error(
            400, "validation_error", "enter a new secret for changed endpoint/auth", "secret"
        )
    will_have_secret = directive == "set" or (
        directive == "keep" and existing.get("secret_ciphertext") is not None
    )
    validate_mcp_fields(
        name=str(merged["name"]),
        description=str(merged["description"]),
        url=str(merged["url"]),
        auth_type=str(merged["auth_type"]),
        auth_header_name=merged["auth_header_name"],
        has_secret=will_have_secret,
    )
    if directive == "set":
        merged["secret"] = patch["secret"]
    return merged, directive


@router.get(
    "/mcp-servers",
    response_model=MCPServerListResponse,
    response_description="The tenant's MCP servers (secrets omitted).",
)
async def list_mcp_servers(
    request: Request, principal: Principal = Depends(resolve_principal)
) -> dict[str, Any]:
    """List all MCP servers owned by the caller's tenant.

    Tenant-scoped read (any authenticated member/API key). The stored auth
    secret is never returned — each row exposes only ``secret_set`` (whether a
    secret is configured). Sorted by name.
    """
    async with request.app.state.session_factory() as session:
        result = await session.execute(
            select(*_ROW_COLS)
            .where(mcp_servers.c.tenant_id == principal.tenant_id)
            .order_by(mcp_servers.c.name)
        )
        rows = [dict(r._mapping) for r in result.fetchall()]
    return {"mcp_servers": [mcp_public_view(r) for r in rows]}


@router.get(
    "/mcp-servers/{mid}",
    response_description="The MCP server (secret omitted).",
)
async def get_mcp_server(
    mid: Annotated[str, Path(description="MCP server id.")],
    request: Request,
    principal: Principal = Depends(resolve_principal),
) -> dict[str, Any]:
    """Fetch a single MCP server by id.

    Tenant-scoped read (any authenticated member/API key); a server belonging to
    another tenant is treated as absent. The auth secret is never returned, only
    ``secret_set``. Returns ``404 not_found`` if no such server exists for the
    tenant.
    """
    async with request.app.state.session_factory() as session:
        result = await session.execute(
            select(*_ROW_COLS).where(
                mcp_servers.c.id == mid, mcp_servers.c.tenant_id == principal.tenant_id
            )
        )
        row = result.fetchone()
    if row is None:
        raise not_found("mcp server not found")
    return mcp_detail_view(dict(row._mapping))


@router.post(
    "/mcp-servers",
    response_description="The created MCP server (secret omitted).",
)
async def create_mcp_server(
    request: Request, principal: Principal = Depends(require_admin)
) -> dict[str, Any]:
    """Register a new MCP server for the caller's tenant.

    Admin-only and tenant-scoped. Body requires ``name``, ``description`` and
    ``url``; optional ``auth_type`` (default ``none``), ``auth_header_name``,
    ``secret`` and ``enabled`` (default true). Any ``secret`` is encrypted at
    rest and never returned. Errors: ``403 forbidden`` for a staff principal
    with no tenant; ``400 validation_error`` for a malformed payload; ``409
    conflict`` if a server of that name already exists for the tenant.
    """
    if principal.tenant_id is None:
        raise api_error(403, "forbidden", "MCP servers are tenant-scoped")
    fields = parse_mcp_create(await request.json())
    row = build_mcp_row(
        tenant_id=principal.tenant_id,
        created_by=principal.user_id,
        name=fields["name"],
        description=fields["description"],
        url=fields["url"],
        auth_type=fields["auth_type"],
        auth_header_name=fields["auth_header_name"],
        secret=fields["secret"],
        enabled=fields["enabled"],
        master_key=load_key_from_env() if fields["secret"] else None,
    )
    async with request.app.state.session_factory() as session:
        dupe = await session.execute(
            select(mcp_servers.c.id).where(
                mcp_servers.c.tenant_id == principal.tenant_id,
                mcp_servers.c.name == row["name"],
            )
        )
        if dupe.fetchone() is not None:
            raise api_error(
                409, "conflict", f"an mcp server named {row['name']!r} already exists", "name"
            )
        await session.execute(mcp_servers.insert().values(**row))
        await session.commit()
    return mcp_detail_view(row)


@router.patch(
    "/mcp-servers/{mid}",
    response_description="The updated MCP server (secret omitted).",
)
async def patch_mcp_server(
    mid: Annotated[str, Path(description="MCP server id.")],
    request: Request,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Update an existing MCP server (partial patch).

    Admin-only and tenant-scoped. Any of ``name``, ``description``, ``url``,
    ``auth_type``, ``auth_header_name`` and ``enabled`` may be patched. The
    ``secret`` field is directive-based: omit it to keep the stored secret, send
    a non-empty value to replace it (re-encrypted at rest), or send an empty
    value to clear it. The secret is never returned. Errors: ``404 not_found``
    if the server does not exist for the tenant; ``409 conflict`` if a rename
    collides with another server's name.
    """
    patch = await request.json()
    async with request.app.state.session_factory() as session:
        result = await session.execute(
            select(*_ROW_COLS).where(
                mcp_servers.c.id == mid, mcp_servers.c.tenant_id == principal.tenant_id
            )
        )
        row = result.fetchone()
        if row is None:
            raise not_found("mcp server not found")
        existing = dict(row._mapping)
        merged, directive = apply_mcp_patch(existing, patch)
        if merged["name"] != existing["name"]:
            dupe = await session.execute(
                select(mcp_servers.c.id).where(
                    mcp_servers.c.tenant_id == principal.tenant_id,
                    mcp_servers.c.name == merged["name"],
                    mcp_servers.c.id != mid,
                )
            )
            if dupe.fetchone() is not None:
                raise api_error(
                    409,
                    "conflict",
                    f"an mcp server named {merged['name']!r} already exists",
                    "name",
                )
        values: dict[str, Any] = {
            "name": merged["name"],
            "description": merged["description"],
            "url": merged["url"],
            "auth_type": merged["auth_type"],
            "auth_header_name": merged["auth_header_name"],
            "enabled": merged["enabled"],
            "updated_at": datetime.now(UTC),
        }
        if directive == "set":
            values["secret_ciphertext"] = encrypt_secret(merged["secret"], load_key_from_env())
        elif directive == "clear":
            values["secret_ciphertext"] = None
        await session.execute(mcp_servers.update().where(mcp_servers.c.id == mid).values(**values))
        await session.commit()
        existing.update(values)
    return mcp_detail_view(existing)


@router.delete(
    "/mcp-servers/{mid}",
    status_code=204,
    response_description="MCP server deleted; no content returned.",
)
async def delete_mcp_server(
    mid: Annotated[str, Path(description="MCP server id.")],
    request: Request,
    principal: Principal = Depends(require_admin),
) -> None:
    """Delete an MCP server by id.

    Admin-only and tenant-scoped. Returns ``204 No Content`` on success.
    Returns ``404 not_found`` if no such server exists for the tenant.
    """
    async with request.app.state.session_factory() as session:
        result = await session.execute(
            select(mcp_servers.c.id).where(
                mcp_servers.c.id == mid, mcp_servers.c.tenant_id == principal.tenant_id
            )
        )
        if result.fetchone() is None:
            raise not_found("mcp server not found")
        await session.execute(mcp_servers.delete().where(mcp_servers.c.id == mid))
        await session.commit()
