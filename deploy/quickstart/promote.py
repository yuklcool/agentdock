"""Promote verified image digests to a release tag, refusing to replace existing content."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path


def inspect(ref):
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", ref],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if b"not found" in result.stderr.lower() or b"manifest unknown" in result.stderr.lower():
            return None
        raise RuntimeError(f"Cannot inspect {ref}: {result.stderr.decode()}")
    return hashlib.sha256(result.stdout).hexdigest()


def main():
    sha = sys.argv[1]
    version = Path(__file__).with_name("VERSION").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", version
    ):
        raise ValueError("Invalid revision or release version")
    components = (
        "agent-runtime",
        "control-plane",
        "web-console",
        "postgres",
        "connectors",
        "egress-proxy",
        "searxng",
        "reverse-proxy",
    )
    plans = []
    for component in components:
        repo = f"ghcr.io/yuklcool/agentdock/{component}"
        candidate, target = f"{repo}:sha-{sha}", f"{repo}:{version}"
        expected, existing = inspect(candidate), inspect(target)
        if expected is None:
            raise RuntimeError(f"Candidate missing: {candidate}")
        if existing is not None and existing != expected:
            raise RuntimeError(f"Release tag exists with different content: {target}; bump VERSION")
        plans.append((candidate, target, expected, existing))
    for candidate, target, expected, existing in plans:
        if existing is None:
            subprocess.run(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "create",
                    "--prefer-index=false",
                    "--tag",
                    target,
                    candidate,
                ],
                check=True,
            )
        if inspect(target) != expected:
            raise RuntimeError(f"Promoted digest mismatch: {target}")
        print(f"Published {target}")


if __name__ == "__main__":
    main()
