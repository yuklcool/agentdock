"""Opt-in encrypted backup/restore against disposable Docker resources."""

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    prefix = "agentdock-backup-test-" + uuid.uuid4().hex[:8]
    source, dest, cp, agent, volume = [
        prefix + suffix for suffix in ("-pg", "-restore", "-cp", "-agent", "-volume")
    ]
    try:
        for name in (source, dest):
            run(
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "com.docker.compose.project=" + name,
                "-e",
                "POSTGRES_PASSWORD=test-only",
                "postgres:16",
                stdout=subprocess.DEVNULL,
            )
            for _ in range(60):
                if (
                    subprocess.run(
                        ["docker", "exec", name, "pg_isready", "-U", "postgres"],
                        capture_output=True,
                    ).returncode
                    == 0
                ):
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError("Postgres unavailable")
        run(
            "docker",
            "run",
            "-d",
            "--name",
            cp,
            "--label",
            "com.docker.compose.project=" + source,
            "--label",
            "com.docker.compose.service=control-plane",
            "alpine:3.21",
            "sleep",
            "infinity",
            stdout=subprocess.DEVNULL,
        )
        run(
            "docker",
            "run",
            "-d",
            "--name",
            agent,
            "-v",
            f"{volume}:/data",
            "alpine:3.21",
            "sleep",
            "infinity",
            stdout=subprocess.DEVNULL,
        )
        run(
            "docker",
            "exec",
            agent,
            "sh",
            "-ec",
            "mkdir -p /data/.agent-state/nanobot; "
            "echo private-history > /data/.agent-state/nanobot/session; "
            "chmod 600 /data/.agent-state/nanobot/session; "
            "chown 1000:1000 /data/.agent-state/nanobot/session",
        )
        run(
            "docker",
            "exec",
            source,
            "psql",
            "-U",
            "postgres",
            "-c",
            "CREATE TABLE containers(id text,docker_name text,volume_name text,status text); "
            f"INSERT INTO containers VALUES ('test','{agent}','{volume}','running')",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity, archive = root / "key", root / "snapshot.age"
            run("age-keygen", "-o", str(identity), capture_output=True)
            recipient = (
                subprocess.check_output(["age-keygen", "-y", str(identity)]).decode().strip()
            )
            run(
                sys.executable,
                "scripts/backup.py",
                "backup",
                "--postgres",
                source,
                "--recipient",
                recipient,
                "--output",
                str(archive),
            )
            assert archive.read_bytes().startswith(b"age-encryption.org/")
            for name in (agent, cp):
                state = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]["State"]
                assert state["Running"] and not state["Paused"]
            run("docker", "rm", "-f", agent, stdout=subprocess.DEVNULL)
            run("docker", "volume", "rm", volume, stdout=subprocess.DEVNULL)
            run(
                sys.executable,
                "scripts/backup.py",
                "restore",
                "--postgres",
                dest,
                "--identity",
                str(identity),
                "--input",
                str(archive),
            )
            count = subprocess.check_output(
                [
                    "docker",
                    "exec",
                    dest,
                    "psql",
                    "-U",
                    "postgres",
                    "-Atc",
                    "SELECT count(*) FROM containers",
                ]
            )
            assert count.strip() == b"1"
            content = subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{volume}:/data:ro",
                    "alpine:3.21",
                    "cat",
                    "/data/.agent-state/nanobot/session",
                ]
            )
            assert content == b"private-history\n"
            mode = subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{volume}:/data:ro",
                    "alpine:3.21",
                    "stat",
                    "-c",
                    "%u:%g:%a",
                    "/data/.agent-state/nanobot/session",
                ]
            )
            assert mode.strip() == b"1000:1000:600"
            print("Encrypted database + private volume backup/restore verified")
    finally:
        for name in (agent, cp, source, dest):
            subprocess.run(["docker", "rm", "-fv", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


if __name__ == "__main__":
    main()
