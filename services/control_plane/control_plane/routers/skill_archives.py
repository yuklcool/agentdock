"""Archive upload endpoints for the tenant skill library."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from control_plane.auth.principal import Principal, require_admin
from control_plane.errors import api_error
from control_plane.models_db import skills
from control_plane.skills_archive import MAX_ARCHIVE_BYTES, scan_skill_archive
from control_plane.skills_service import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    build_archive_skill_row,
    skill_public_view,
)

router = APIRouter(tags=["Skills"])


async def _read_archive_body(request: Request) -> bytes:
    """Read a raw archive request body without allowing an oversized upload."""
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_ARCHIVE_BYTES:
            raise api_error(
                413,
                "archive_too_large",
                f"skill archive exceeds {MAX_ARCHIVE_BYTES} upload bytes",
                "file",
            )
    if not payload:
        raise api_error(400, "validation_error", "skill archive is empty", "file")
    return bytes(payload)


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    if not name or name in {".", ".."}:
        raise api_error(400, "validation_error", "filename is required", "filename")
    if len(name) > 255:
        raise api_error(400, "validation_error", "filename is too long", "filename")
    return name


async def _scan(request: Request, filename: str):
    data = await _read_archive_body(request)
    try:
        return await asyncio.to_thread(
            scan_skill_archive,
            data,
            filename=filename,
            max_files=MAX_BUNDLE_FILES,
            max_bytes=MAX_BUNDLE_BYTES,
        )
    except ValueError as exc:
        raise api_error(
            422, "skill_archive_error", str(exc), "file"
        ) from exc


async def _installed_names(request: Request, tenant_id: str) -> set[str]:
    async with request.app.state.session_factory() as session:
        result = await session.execute(
            select(skills.c.name).where(skills.c.tenant_id == tenant_id)
        )
        return {str(row._mapping["name"]) for row in result.fetchall()}


@router.post(
    "/skills/archive-discover",
    response_description="Skills discovered in an uploaded ZIP/TAR.GZ archive.",
)
async def discover_skill_archive(
    request: Request,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Validate an archive and preview every discovered SKILL.md before import.

    The archive bytes are the raw request body. This avoids multipart parsing
    dependencies and lets the server enforce the compressed upload limit while
    streaming the body. Supported filenames: .zip, .tar.gz and .tgz.
    """
    if principal.tenant_id is None:
        raise api_error(403, "forbidden", "Skills are tenant-scoped")
    source_filename = _safe_filename(filename)
    scan = await _scan(request, source_filename)
    installed = await _installed_names(request, principal.tenant_id)
    return {
        "ok": True,
        "truncated": scan.truncated,
        "skills": [
            {
                "subpath": item.subpath,
                "name": item.name,
                "description": item.description,
                "valid": item.valid,
                "error": item.error,
                "installed": item.valid and item.name in installed,
                "bundle_size": item.bundle_size,
            }
            for item in scan.skills
        ],
    }


@router.post(
    "/skills/archive-import",
    response_description="The skills imported from an uploaded ZIP/TAR.GZ archive.",
)
async def import_skill_archive(
    request: Request,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    selected: Annotated[list[str], Query()],
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Import selected skill subpaths from an archive after a preview.

    `selected` is a repeated query parameter containing the subpaths returned by
    `/skills/archive-discover`. The archive bytes are sent again as the raw
    request body; the server re-validates them so import never trusts client
    preview state.
    """
    if principal.tenant_id is None:
        raise api_error(403, "forbidden", "Skills are tenant-scoped")
    source_filename = _safe_filename(filename)
    chosen = list(dict.fromkeys(selected))
    if not chosen:
        raise api_error(
            400, "validation_error", "select at least one skill", "selected"
        )

    scan = await _scan(request, source_filename)
    by_subpath = {item.subpath: item for item in scan.skills}
    missing = [subpath for subpath in chosen if subpath not in by_subpath]
    if missing:
        raise api_error(
            422,
            "validation_error",
            "selected skill was not found in archive: " + ", ".join(missing[:5]),
            "selected",
        )
    invalid = [by_subpath[subpath] for subpath in chosen if not by_subpath[subpath].valid]
    if invalid:
        first = invalid[0]
        raise api_error(
            422,
            "skill_archive_error",
            f"selected skill {first.subpath or '/'} is invalid: {first.error}",
            "selected",
        )

    installed = await _installed_names(request, principal.tenant_id)
    conflicts = [
        by_subpath[subpath].name
        for subpath in chosen
        if by_subpath[subpath].name in installed
    ]
    if conflicts:
        raise api_error(
            409,
            "conflict",
            "skills already exist: " + ", ".join(conflicts[:10]),
            "selected",
        )

    rows = [
        build_archive_skill_row(
            tenant_id=principal.tenant_id,
            created_by=principal.user_id,
            enabled=True,
            source_filename=source_filename,
            archived=by_subpath[subpath],
        )
        for subpath in chosen
    ]

    async with request.app.state.session_factory() as session:
        try:
            for row in rows:
                await session.execute(skills.insert().values(**row))
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise api_error(
                409,
                "conflict",
                "one or more skill names were installed concurrently",
                "selected",
            ) from exc

    return {
        "ok": True,
        "imported": [skill_public_view(row) for row in rows],
    }
