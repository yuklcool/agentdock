"""Safely inspect and pack skills from uploaded ZIP/TAR.GZ archives.

Archives are treated as another source for the existing Skill bundle format.
Extraction is deliberately manual: paths, entry types and expansion limits are
validated before any bytes are written to the temporary directory.
"""

from __future__ import annotations

import io
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from agentcore.drivers.skills_md import MAX_DESCRIPTION, valid_skill_name
from control_plane.skills_fetch import pack_dir, parse_skill_frontmatter

MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_FILES = 1_000
MAX_ARCHIVE_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_SINGLE_FILE_BYTES = 25 * 1024 * 1024
MAX_DISCOVERED_SKILLS = 50


@dataclass(frozen=True)
class ArchivedSkill:
    subpath: str
    name: str
    description: str
    body: str
    valid: bool
    error: str | None
    bundle: bytes | None = None
    bundle_sha256: str | None = None
    bundle_size: int | None = None


@dataclass(frozen=True)
class ArchiveScan:
    truncated: bool
    skills: list[ArchivedSkill]


def _safe_relative_path(raw_name: str) -> Path:
    """Return a platform path for one safe, relative archive member name."""
    if "\x00" in raw_name:
        raise ValueError("archive contains a NUL byte in a path")
    normalized = raw_name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        raise ValueError(f"archive contains an absolute path: {raw_name!r}")
    parts = tuple(part for part in pure.parts if part not in ("", "."))
    if not parts:
        return Path(".")
    if any(part == ".." for part in parts):
        raise ValueError(f"archive path traversal rejected: {raw_name!r}")
    if ":" in parts[0]:
        raise ValueError(f"archive contains a drive-qualified path: {raw_name!r}")
    return Path(*parts)


def _validate_expansion(*, file_count: int, total_size: int, size: int) -> None:
    if file_count > MAX_ARCHIVE_FILES:
        raise ValueError(f"archive exceeds {MAX_ARCHIVE_FILES} files")
    if size > MAX_ARCHIVE_SINGLE_FILE_BYTES:
        raise ValueError(
            f"archive member exceeds {MAX_ARCHIVE_SINGLE_FILE_BYTES} bytes"
        )
    if total_size > MAX_ARCHIVE_EXPANDED_BYTES:
        raise ValueError(
            f"archive expands beyond {MAX_ARCHIVE_EXPANDED_BYTES} bytes"
        )


def _extract_zip(data: bytes, root: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        file_count = 0
        total_size = 0
        for info in infos:
            rel = _safe_relative_path(info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"archive symlink rejected: {info.filename!r}")
            if info.is_dir():
                (root / rel).mkdir(parents=True, exist_ok=True)
                continue
            # Some creators do not set a Unix file type; zero is a regular file
            # for our purposes. Explicit special types are rejected.
            file_type = stat.S_IFMT(mode)
            if file_type not in (0, stat.S_IFREG):
                raise ValueError(
                    f"archive special file rejected: {info.filename!r}"
                )
            file_count += 1
            total_size += info.file_size
            _validate_expansion(
                file_count=file_count, total_size=total_size, size=info.file_size
            )
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as dest:
                written = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > info.file_size or written > MAX_ARCHIVE_SINGLE_FILE_BYTES:
                        raise ValueError(
                            f"archive member expanded beyond declared size: {info.filename!r}"
                        )
                    dest.write(chunk)


def _extract_tar(data: bytes, root: Path) -> None:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise ValueError("invalid tar archive") from exc

    with archive:
        members = archive.getmembers()
        file_count = 0
        total_size = 0
        for member in members:
            rel = _safe_relative_path(member.name)
            if member.isdir():
                (root / rel).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(
                    f"archive link or special file rejected: {member.name!r}"
                )
            file_count += 1
            total_size += member.size
            _validate_expansion(
                file_count=file_count, total_size=total_size, size=member.size
            )
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"could not read archive member: {member.name!r}")
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as dest:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError(
                            f"archive member ended early: {member.name!r}"
                        )
                    dest.write(chunk)
                    remaining -= len(chunk)


def _extract_archive(data: bytes, filename: str, root: Path) -> None:
    if not data:
        raise ValueError("archive is empty")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"archive exceeds {MAX_ARCHIVE_BYTES} upload bytes")

    lower = Path(filename).name.lower()
    if lower.endswith(".zip"):
        try:
            _extract_zip(data, root)
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid zip archive") from exc
        return
    if lower.endswith((".tar.gz", ".tgz")):
        _extract_tar(data, root)
        return
    raise ValueError("archive must be .zip, .tar.gz, or .tgz")


def scan_skill_archive(
    data: bytes,
    *,
    filename: str,
    max_files: int,
    max_bytes: int,
) -> ArchiveScan:
    """Extract an upload safely, discover SKILL.md files and pack valid skills."""
    with tempfile.TemporaryDirectory(prefix="agentdock-skill-upload-") as tmp:
        root = Path(tmp)
        _extract_archive(data, filename, root)

        manifests = sorted(root.rglob("SKILL.md"))
        if not manifests:
            raise ValueError("archive contains no SKILL.md")
        truncated = len(manifests) > MAX_DISCOVERED_SKILLS
        manifests = manifests[:MAX_DISCOVERED_SKILLS]

        found: list[ArchivedSkill] = []
        for manifest in manifests:
            subpath = manifest.parent.relative_to(root).as_posix()
            if subpath == ".":
                subpath = ""
            try:
                md = manifest.read_text(encoding="utf-8")
                name, description, body = parse_skill_frontmatter(md)
                if not valid_skill_name(name):
                    raise ValueError(
                        "SKILL.md name must match ^[a-z0-9]+(-[a-z0-9]+)*$ "
                        "and be 1-64 chars"
                    )
                if len(description) > MAX_DESCRIPTION:
                    raise ValueError(
                        f"SKILL.md description exceeds {MAX_DESCRIPTION} chars"
                    )
                bundle, bundle_size, bundle_sha256 = pack_dir(
                    manifest.parent, max_files=max_files, max_bytes=max_bytes
                )
                found.append(
                    ArchivedSkill(
                        subpath=subpath,
                        name=name,
                        description=description,
                        body=body,
                        valid=True,
                        error=None,
                        bundle=bundle,
                        bundle_sha256=bundle_sha256,
                        bundle_size=bundle_size,
                    )
                )
            except (OSError, UnicodeError, ValueError) as exc:
                found.append(
                    ArchivedSkill(
                        subpath=subpath,
                        name=manifest.parent.name or "(root)",
                        description="",
                        body="",
                        valid=False,
                        error=str(exc),
                    )
                )

        counts: dict[str, int] = {}
        for skill in found:
            if skill.valid:
                counts[skill.name] = counts.get(skill.name, 0) + 1
        found = [
            replace(
                skill,
                valid=False,
                error=f"duplicate skill name {skill.name!r} in archive",
                bundle=None,
                bundle_sha256=None,
                bundle_size=None,
            )
            if skill.valid and counts.get(skill.name, 0) > 1
            else skill
            for skill in found
        ]

        return ArchiveScan(truncated=truncated, skills=found)
