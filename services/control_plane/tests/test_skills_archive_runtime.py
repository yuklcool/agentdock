from __future__ import annotations

import base64

import pytest

from control_plane.skills_archive import ArchivedSkill
from control_plane.skills_service import build_archive_skill_row, resolve_skills_for_request

pytestmark = pytest.mark.unit


def test_archive_skill_row_keeps_bundle_and_source_metadata() -> None:
    archived = ArchivedSkill(
        subpath="lighting/sql",
        name="lighting-sql",
        description="SQL analysis",
        body="# Instructions",
        valid=True,
        error=None,
        bundle=b"bundle-bytes",
        bundle_sha256="a" * 64,
        bundle_size=12,
    )
    row = build_archive_skill_row(
        tenant_id="ten_1",
        created_by="usr_1",
        enabled=True,
        source_filename="lighting.zip",
        archived=archived,
    )
    assert row["source_type"] == "archive"
    assert row["source_ref"] == "lighting.zip"
    assert row["source_subpath"] == "lighting/sql"
    assert row["bundle"] == b"bundle-bytes"
    assert row["bundle_size"] == 12


def test_archive_skill_resolves_as_bundle_not_inline_body() -> None:
    rows = [
        {
            "id": "skl_archive",
            "name": "lighting-sql",
            "description": "SQL analysis",
            "body": "this body must not be used when a bundle exists",
            "enabled": True,
            "source_type": "archive",
            "bundle": b"bundle-bytes",
            "bundle_size": 12,
        }
    ]
    resolved = resolve_skills_for_request(["skl_archive"], rows)
    assert len(resolved) == 1
    assert resolved[0].bundle_b64 == base64.b64encode(b"bundle-bytes").decode()
    assert resolved[0].body == ""
