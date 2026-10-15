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

    def docker(*parts, check=True, timeout=30):
        return subprocess.run(
            ["docker", *parts], check=check, capture_output=True, text=True, timeout=timeout
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
        # Exercise the real protected initialization API with the production
        # read-only root, uid drop and capability set (not a root-only CLI test).
        def initialize(soul):
            response = docker(
                "exec", name, "curl", "-fsS", "-H", "Authorization: Bearer image-test-only",
                "-H", "Content-Type: application/json", "-d",
                json.dumps({"files": {"SOUL.md": soul}}),
                "http://127.0.0.1:8080/nanobot/initialize", timeout=90,
            )
            return json.loads(response.stdout)

        assert initialize("Image initialization soul") == {"initialized": True}
        assert initialize("Must not reapply") == {"initialized": False}
        docker("exec", name, "python3", "-c", (
            "import json,pathlib; "
            "p=pathlib.Path('/workspace/.agent-runtime/nanobot-init/config.json'); "
            "assert p.stat().st_uid == 0 and p.stat().st_mode & 0o777 == 0o600; "
            "c=json.loads(p.read_text()); "
            "assert c['agents']['defaults']['workspace'] == '/workspace/nanobot'; "
            "w=pathlib.Path('/workspace/nanobot'); "
            "assert (w/'SOUL.md').read_text() == 'Image initialization soul'; "
            "assert (w/'AGENTS.md').exists(); "
            "assert (w/'SOUL.md').stat().st_uid == 1000"
        ))
        docker(
            "exec",
            "--user",
            "1000:1000",
            name,
            "/bin/sh",
            "-ec",
            "test ! -r /workspace/.agent-runtime/tasks; "
            "test ! -r /workspace/.agent-runtime/nanobot-init/config.json; "
            "test -w /workspace/.agent-state; "
            "/app/.venv/bin/nanobot --help >/dev/null; node --version",
        )
        print("Shim entrypoint, native initialization, authentication and uid boundaries passed")
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", name, check=False)


if __name__ == "__main__":
    main()
