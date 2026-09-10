from __future__ import annotations

import io
import stat
import tarfile
import zipfile

import pytest

from control_plane.skills_archive import scan_skill_archive

pytestmark = pytest.mark.unit


def _manifest(name: str, description: str = "A test skill") -> bytes:
    return (
        f"---\nname: {name}\ndescription: \"{description}\"\n---\n# Instructions\n"
    ).encode()


def _zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
    return out.getvalue()


def _tar_gz(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for path, data in entries.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _scan(data: bytes, filename: str):
    return scan_skill_archive(
        data,
        filename=filename,
        max_files=200,
        max_bytes=5_000_000,
    )


def test_zip_single_skill_at_root() -> None:
    result = _scan(
        _zip({"SKILL.md": _manifest("pdf-tools"), "scripts/run.sh": b"echo hi\n"}),
        "pdf-tools.zip",
    )
    assert result.truncated is False
    assert len(result.skills) == 1
    skill = result.skills[0]
    assert skill.valid is True
    assert skill.subpath == ""
    assert skill.name == "pdf-tools"
    assert skill.bundle
    assert skill.bundle_size and skill.bundle_size > 0
    assert skill.bundle_sha256 and len(skill.bundle_sha256) == 64


def test_zip_discovers_multiple_skills_under_wrapper_directory() -> None:
    result = _scan(
        _zip(
            {
                "lighting-pack/sql/SKILL.md": _manifest("lighting-sql", "SQL analysis"),
                "lighting-pack/sql/ref/schema.md": b"schema",
                "lighting-pack/health/SKILL.md": _manifest("lighting-health", "Health analysis"),
            }
        ),
        "lighting-pack.zip",
    )
    assert [(s.subpath, s.name, s.valid) for s in result.skills] == [
        ("lighting-pack/health", "lighting-health", True),
        ("lighting-pack/sql", "lighting-sql", True),
    ]


@pytest.mark.parametrize("filename", ["skills.tar.gz", "skills.tgz"])
def test_tar_gz_and_tgz_are_supported(filename: str) -> None:
    result = _scan(
        _tar_gz({"alpha/SKILL.md": _manifest("alpha")}),
        filename,
    )
    assert len(result.skills) == 1
    assert result.skills[0].valid is True
    assert result.skills[0].name == "alpha"


def test_invalid_skill_is_reported_without_aborting_other_skills() -> None:
    result = _scan(
        _zip(
            {
                "good/SKILL.md": _manifest("good"),
                "bad/SKILL.md": b"# missing frontmatter\n",
            }
        ),
        "mixed.zip",
    )
    by_path = {skill.subpath: skill for skill in result.skills}
    assert by_path["good"].valid is True
    assert by_path["bad"].valid is False
    assert "frontmatter" in (by_path["bad"].error or "")


def test_duplicate_skill_names_are_invalidated() -> None:
    result = _scan(
        _zip(
            {
                "one/SKILL.md": _manifest("same-name"),
                "two/SKILL.md": _manifest("same-name"),
            }
        ),
        "duplicate.zip",
    )
    assert len(result.skills) == 2
    assert all(not skill.valid for skill in result.skills)
    assert all("duplicate skill name" in (skill.error or "") for skill in result.skills)


def test_zip_path_traversal_is_rejected() -> None:
    with pytest.raises(ValueError, match="path traversal"):
        _scan(
            _zip({"../escape/SKILL.md": _manifest("escape")}),
            "escape.zip",
        )


def test_zip_symlink_is_rejected() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        info = zipfile.ZipInfo("skill/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../../outside")
        archive.writestr("skill/SKILL.md", _manifest("skill"))
    with pytest.raises(ValueError, match="symlink"):
        _scan(out.getvalue(), "symlink.zip")


def test_tar_symlink_is_rejected() -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        manifest = _manifest("skill")
        manifest_info = tarfile.TarInfo("skill/SKILL.md")
        manifest_info.size = len(manifest)
        archive.addfile(manifest_info, io.BytesIO(manifest))
        link = tarfile.TarInfo("skill/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
    with pytest.raises(ValueError, match="link or special file"):
        _scan(out.getvalue(), "symlink.tar.gz")


def test_archive_without_skill_manifest_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"no SKILL\.md"):
        _scan(_zip({"README.md": b"nothing"}), "empty.zip")


def test_per_skill_bundle_limits_are_reused() -> None:
    data = _zip(
        {
            "skill/SKILL.md": _manifest("skill"),
            "skill/large.txt": b"x" * 256,
        }
    )
    result = scan_skill_archive(
        data,
        filename="skill.zip",
        max_files=200,
        max_bytes=64,
    )
    assert result.skills[0].valid is False
    assert "exceeds 64 bytes" in (result.skills[0].error or "")
