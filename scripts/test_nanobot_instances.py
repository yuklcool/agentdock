#!/usr/bin/env python3
"""Three real containers, private volumes, pause/resume and volume re-creation."""

from __future__ import annotations

import argparse
import concurrent.futures
import subprocess
import time
import uuid
from pathlib import Path


def run(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    prefix = "agentdock-acceptance-" + uuid.uuid4().hex[:8]
    test = Path("packages/agentcore/tests/drivers/test_nanobot_live.py").resolve()
    names = [f"{prefix}-{i}" for i in range(3)]
    try:
        for i, name in enumerate(names):
            run("volume", "create", name, stdout=subprocess.DEVNULL)
            run(
                "run",
                "-d",
                "--name",
                name,
                "--user",
                "root",
                "-v",
                f"{name}:/workspace",
                "-v",
                f"{test}:/tests/test_nanobot_live.py:ro",
                "-e",
                "AGENTDOCK_TEST_NANOBOT=1",
                "-e",
                "AGENTDOCK_TEST_WORKSPACE=/workspace",
                "-e",
                f"AGENTDOCK_TEST_INSTANCE={i}",
                "--entrypoint",
                "sleep",
                args.image,
                "infinity",
                stdout=subprocess.DEVNULL,
            )
            # Install test harness once; the runtime remains the actual built image.
            run(
                "exec",
                name,
                "/bin/bash",
                "-ec",
                "/opt/venv/bin/python -m ensurepip && "
                "/opt/venv/bin/python -m pip install pytest pytest-asyncio",
                stdout=subprocess.DEVNULL,
            )

        def exercise(name):
            return run(
                "exec",
                name,
                "/opt/venv/bin/python",
                "-m",
                "pytest",
                "-q",
                "-o",
                "asyncio_mode=auto",
                "/tests/test_nanobot_live.py",
                capture_output=True,
                text=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(exercise, name) for name in names]
            deadline = time.monotonic() + 180
            for name in names:
                while time.monotonic() < deadline:
                    result = subprocess.run(
                        ["docker", "exec", name, "test", "-f", "/workspace/acceptance-ready"]
                    )
                    if result.returncode == 0:
                        break
                    if any(f.done() and f.exception() for f in futures):
                        for f in futures:
                            if f.done():
                                f.result()
                    time.sleep(0.3)
                else:
                    raise TimeoutError("Native gateway acceptance timed out")
                run("pause", name, stdout=subprocess.DEVNULL)
                assert (
                    subprocess.check_output(
                        ["docker", "inspect", "-f", "{{.State.Paused}}", name]
                    ).strip()
                    == b"true"
                )
                run("unpause", name, stdout=subprocess.DEVNULL)
                run("exec", name, "touch", "/workspace/acceptance-resume")
            for future in futures:
                print(future.result().stdout)
        # Re-create containers from their original immutable image and volumes.
        for i, name in enumerate(names):
            run("rm", "-f", name, stdout=subprocess.DEVNULL)
            run(
                "run",
                "--rm",
                "--name",
                name,
                "--user",
                "root",
                "-v",
                f"{name}:/workspace",
                "-v",
                f"{test}:/tests/test_nanobot_live.py:ro",
                "-e",
                "AGENTDOCK_TEST_NANOBOT=1",
                "-e",
                "AGENTDOCK_TEST_WORKSPACE=/workspace",
                "-e",
                "AGENTDOCK_TEST_RESTORE=1",
                "-e",
                f"AGENTDOCK_TEST_INSTANCE={i}",
                "--entrypoint",
                "/bin/bash",
                args.image,
                "-ec",
                "/opt/venv/bin/python -m ensurepip && "
                "/opt/venv/bin/python -m pip install pytest pytest-asyncio && "
                "/opt/venv/bin/python -m pytest -q -o asyncio_mode=auto "
                "/tests/test_nanobot_live.py",
            )
    except subprocess.CalledProcessError as exc:
        print(exc.stdout or "", exc.stderr or "")
        raise
    finally:
        for name in names:
            subprocess.run(
                ["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            subprocess.run(
                ["docker", "volume", "rm", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    main()
