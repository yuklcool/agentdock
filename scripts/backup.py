#!/usr/bin/env python3
"""AgentDock cold backup/restore. Requires docker CLI, Python 3.12 and age.

Backup stops every CP replica in the Postgres compose project and freezes agent
containers; finally restores their original running state. Restore only accepts
an empty database and absent target volumes, and never starts the control plane.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(args):
    return subprocess.check_output(args)


def inspect(name):
    return json.loads(output(["docker", "inspect", name]))[0]


def pg_args(container):
    env = dict(item.split("=", 1) for item in inspect(container)["Config"]["Env"])
    url = urlsplit(env.get("DATABASE_URL", "").replace("postgresql+asyncpg:", "postgresql:"))
    return [
        "-U",
        env.get("POSTGRES_USER") or unquote(url.username or "postgres"),
        "-d",
        env.get("POSTGRES_DB") or unquote(url.path.lstrip("/") or "postgres"),
    ]


def query(pg, sql):
    return output(
        ["docker", "exec", pg, "psql", *pg_args(pg), "-v", "ON_ERROR_STOP=1", "-Atc", sql]
    )


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_manifest(root):
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("format") != 1:
        raise ValueError("Unsupported backup format")
    for name, checksum in manifest["sha256"].items():
        if Path(name).name != name or digest(root / name) != checksum:
            raise ValueError("Invalid backup checksum or filename")
    for volume in manifest["volumes"]:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]+", volume):
            raise ValueError("Invalid Docker volume name")
        if f"{volume}.tar" not in manifest["sha256"]:
            raise ValueError("Missing volume checksum")
    if "database.dump" not in manifest["sha256"]:
        raise ValueError("Missing database checksum")
    return manifest


def backup(args, root):
    pg = inspect(args.postgres)
    project = pg["Config"].get("Labels", {}).get("com.docker.compose.project")
    if not project:
        raise ValueError("Postgres must belong to the AgentDock Compose project")
    ids = (
        output(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=control-plane",
            ]
        )
        .decode()
        .split()
    )
    if not ids:
        raise ValueError("No running control plane found in this Compose project")
    stopped, paused = [], []
    try:
        for cid in ids:
            command(["docker", "stop", "--time", "30", cid], stdout=subprocess.DEVNULL)
            stopped.append(cid)
        rows = json.loads(
            query(
                args.postgres,
                "SELECT coalesce(json_agg(json_build_object('id',id,'docker_name',docker_name,"
                "'volume_name',volume_name)), '[]') FROM containers WHERE status <> 'destroyed'",
            )
        )
        for row in rows:
            found = (
                output(["docker", "ps", "-aq", "--filter", f"name=^{row['docker_name']}$"])
                .decode()
                .strip()
            )
            if found and inspect(found)["State"]["Status"] == "running":
                command(["docker", "pause", found], stdout=subprocess.DEVNULL)
                paused.append(found)
        # All known writers are stopped/frozen before either side of the snapshot.
        with (root / "database.dump").open("wb") as stream:
            command(
                [
                    "docker",
                    "exec",
                    args.postgres,
                    "pg_dump",
                    *pg_args(args.postgres),
                    "-Fc",
                    "--no-owner",
                    "--no-acl",
                ],
                stdout=stream,
            )
        volumes = sorted({row["volume_name"] for row in rows})
        for volume in volumes:
            command(["docker", "volume", "inspect", volume], stdout=subprocess.DEVNULL)
            with (root / f"{volume}.tar").open("wb") as stream:
                command(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "-v",
                        f"{volume}:/data:ro",
                        args.archive_image,
                        "tar",
                        "-C",
                        "/data",
                        "-cpf",
                        "-",
                        ".",
                    ],
                    stdout=stream,
                )
        checksums = {p.name: digest(p) for p in root.iterdir()}
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "format": 1,
                    "volumes": volumes,
                    "instances": rows,
                    "sha256": checksums,
                },
                indent=2,
            )
        )
    finally:
        # Attempt every restart even if one cleanup fails.
        failures = []
        for verb, entries in (("unpause", paused), ("start", stopped)):
            for cid in entries:
                try:
                    command(["docker", verb, cid], stdout=subprocess.DEVNULL)
                except subprocess.CalledProcessError:
                    failures.append(cid)
        if failures:
            raise RuntimeError("Manually restart containers: " + ", ".join(failures))
    archive = root.parent / "snapshot.tar"
    with tarfile.open(archive, "w") as tar:
        for path in sorted(root.iterdir()):
            tar.add(path, arcname=path.name)
    command(["age", "-r", args.recipient, "-o", str(args.output), str(archive)])


def restore(args, root):
    archive = root.parent / "snapshot.tar"
    command(["age", "-d", "-i", str(args.identity), "-o", str(archive), str(args.input)])
    with tarfile.open(archive) as tar:
        # Only regular flat files accepted; no links/devices/path traversal.
        members = tar.getmembers()
        if any(not m.isfile() or Path(m.name).name != m.name for m in members):
            raise ValueError("Invalid backup archive")
        tar.extractall(root, members=members, filter="data")
    manifest = validate_manifest(root)
    pg = inspect(args.postgres)
    project = pg["Config"].get("Labels", {}).get("com.docker.compose.project")
    if (
        project
        and output(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=control-plane",
            ]
        ).strip()
    ):
        raise ValueError("Stop the destination control plane before restore")
    if int(
        query(
            args.postgres,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
        )
    ):
        raise ValueError("Destination database must be empty")
    existing = set(output(["docker", "volume", "ls", "-q"]).decode().split())
    if existing.intersection(manifest["volumes"]):
        raise ValueError("Destination volumes already exist; restore onto a clean host")
    for volume in manifest["volumes"]:
        command(["docker", "volume", "create", volume], stdout=subprocess.DEVNULL)
        with (root / f"{volume}.tar").open("rb") as stream:
            command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-i",
                    "--network",
                    "none",
                    "--read-only",
                    "-v",
                    f"{volume}:/data",
                    args.archive_image,
                    "tar",
                    "-C",
                    "/data",
                    "-xpf",
                    "-",
                ],
                stdin=stream,
            )
    with (root / "database.dump").open("rb") as stream:
        command(
            [
                "docker",
                "exec",
                "-i",
                args.postgres,
                "pg_restore",
                *pg_args(args.postgres),
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-acl",
            ],
            stdin=stream,
        )
    print(
        "Restored. Install the original encryption key and runtime image before starting AgentDock."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("--postgres", required=True, help="Postgres Docker container name")
    parser.add_argument("--archive-image", default="alpine:3.21")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--recipient")
    parser.add_argument("--identity", type=Path)
    args = parser.parse_args()
    if args.action == "backup" and (not args.output or not args.recipient):
        parser.error("backup requires --output and --recipient")
    if args.action == "restore" and (not args.input or not args.identity):
        parser.error("restore requires --input and --identity")
    if args.output and args.output.exists():
        parser.error("Refusing to overwrite an existing backup")
    os.umask(0o077)
    command(["age", "--version"], stdout=subprocess.DEVNULL)
    command(["docker", "image", "inspect", args.archive_image], stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(prefix="agentdock-backup-") as tmp:
        root = Path(tmp) / "data"
        root.mkdir(mode=0o700)
        (backup if args.action == "backup" else restore)(args, root)


if __name__ == "__main__":
    main()
