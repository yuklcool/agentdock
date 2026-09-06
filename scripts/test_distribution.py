"""Live acceptance against the pulled Compose stack; no source installs or model secrets."""

from __future__ import annotations

import http.cookiejar
import json
import subprocess
import urllib.request
from pathlib import Path


def main():
    directory = Path("deploy/quickstart")
    env = dict(
        line.split("=", 1)
        for line in (directory / ".env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    base = env["PUBLIC_URL"]
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def request(path, data=None, method=None):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with client.open(req, timeout=120) as response:
            return response.read()

    assert b"AgentDock" in request("/")
    assert json.loads(request("/healthz"))["status"] == "ok"
    user = json.loads(
        request(
            "/v1/auth/login",
            {
                "email": env["SEED_STAFF_EMAIL"],
                "password": env["SEED_STAFF_PASSWORD"],
            },
        )
    )
    assert user["is_staff"] and user["must_change_password"]
    request("/v1/auth/select-tenant", {"tenant_id": "ten_seed"})
    container = json.loads(
        request(
            "/v1/containers",
            {
                "name": "distribution-acceptance",
                "visibility": "private",
                "config": {"driver": "nanobot", "model": "gpt-4o"},
            },
        )
    )
    cid = container["id"]
    try:
        assert container["status"] == "running"
        assert container["config"]["driver"] == "nanobot"
        assert container["image_tag"] == env["AGENTDOCK_VERSION"]
        subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "control-plane",
                "curl",
                "-fsS",
                "http://searxng:8080/healthz",
            ],
            cwd=directory,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    finally:
        request(f"/v1/containers/{cid}", method="DELETE")
    print(
        "Pulled distribution: UI, health, administrator login, "
        "private Nanobot provisioning and search passed"
    )


if __name__ == "__main__":
    main()
