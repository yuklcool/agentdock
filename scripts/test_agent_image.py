#!/usr/bin/env python3
"""Exercise the default Shim entrypoint under production filesystem/capability limits."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    name = "agentdock-image-check-" + uuid.uuid4().hex[:8]

    def docker(*parts, check=True):
        return subprocess.run(
            ["docker", *parts], check=check, capture_output=True, text=True, timeout=30
        )

    try:
        docker("volume", "create", name)
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--user",
            "0:0",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "KILL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "256",
            "--memory",
            "512m",
            "--tmpfs",
            "/tmp:size=512m,exec",
            "--tmpfs",
            "/var/tmp:size=128m",
            "--tmpfs",
            "/home/agent:size=256m,exec",
            "-v",
            f"{name}:/workspace",
            "-e",
            "SHIM_TOKEN=image-test-only",
            args.image,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            response = docker(
                "exec", name, "curl", "-fsS", "http://127.0.0.1:8080/readyz", check=False
            )
            if response.returncode == 0:
                assert json.loads(response.stdout)["ready"] is True
                break
            time.sleep(0.5)
        else:
            raise AssertionError("Shim entrypoint did not become ready")

        for headers, expected in [
            ((), "401"),
            (("-H", "Authorization: Bearer image-test-only"), "422"),
        ]:
            response = docker(
                "exec",
                name,
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-H",
                "Content-Type: application/json",
                *headers,
                "-d",
                "{}",
                "http://127.0.0.1:8080/tasks",
            )
            assert response.stdout == expected, response.stdout
        docker(
            "exec",
            "--user",
            "1000:1000",
            name,
            "/bin/sh",
            "-ec",
            "test ! -r /workspace/.agent-runtime/tasks; "
            "test -w /workspace/.agent-state; "
            "/app/.venv/bin/nanobot --help >/dev/null; node --version",
        )
        print("Shim entrypoint, readiness, authentication and uid boundaries passed")
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", name, check=False)


if __name__ == "__main__":
    main()
