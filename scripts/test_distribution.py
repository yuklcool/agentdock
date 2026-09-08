"""Live acceptance against the pulled Compose stack; no source installs or model secrets."""

from __future__ import annotations

import http.cookiejar
import json
import subprocess
import time
import urllib.error
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

    def request(path, data=None, method=None, opener=client):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with opener.open(req, timeout=120) as response:
            return response.read()

    deadline = time.monotonic() + 30
    while True:
        try:
            assert json.loads(request("/healthz"))["status"] == "ok"
            break
        except urllib.error.URLError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)
    assert b"agentdock" in request("/").lower()
    user = json.loads(
        request(
            "/v1/auth/login",
            {
                "email": env["SEED_STAFF_EMAIL"],
                "password": env["SEED_STAFF_PASSWORD"],
            },
        )
    )
    assert user["must_change_password"]
    assert json.loads(request("/v1/auth/me"))["is_staff"]
    request("/v1/auth/select-tenant", {"tenant_id": "ten_seed"})
    # Exercise the migrated schema and endpoint edits against the real API/DB.
    credential = json.loads(request("/v1/credentials", {
        "provider": "openai", "api_key": "distribution-test-only-1234",
        "base_url": "https://proxy.example/v1/",
    }))
    assert credential["base_url"] == "https://proxy.example/v1"
    for endpoint in ["https://second.example/v1", None]:
        updated = json.loads(request(
            f"/v1/credentials/{credential['id']}", {"base_url": endpoint}, method="PATCH",
        ))
        assert updated["last4"] == "1234" and updated["base_url"] == endpoint
        listing = json.loads(request("/v1/credentials"))
        saved = next(c for c in listing["credentials"] if c["id"] == credential["id"])
        assert saved["base_url"] == endpoint
        assert "distribution-test-only" not in json.dumps(listing)
    request(f"/v1/credentials/{credential['id']}", method="DELETE")
    members = []
    for name in ('alice', 'bob'):
        email = f'{name}-distribution@example.com'
        password = 'Disposable-test-password-7392!'
        member = json.loads(request('/v1/users', {
            'name': name, 'email': email, 'password': password, 'role': 'member',
        }))
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        request('/v1/auth/login', {'email': email, 'password': password}, opener=opener)
        request('/v1/auth/select-tenant', {'tenant_id': 'ten_seed'}, opener=opener)
        members.append((member['id'], opener))
    container = json.loads(
        request(
            "/v1/containers",
            {
                "name": "distribution-acceptance",
                "visibility": "private",
                "owner_user_id": members[0][0],
                "config": {"driver": "nanobot", "model": "gpt-4o"},
            },
        )
    )
    cid = container["id"]
    try:
        assert container["status"] == "running"
        assert container["config"]["driver"] == "nanobot"
        assert container["image_tag"] == env["AGENTDOCK_VERSION"]
        assert container['owner_user_id'] == members[0][0]
        for index, (_, opener) in enumerate(members):
            listing = json.loads(request('/v1/containers', opener=opener))['containers']
            assert (cid in {c['id'] for c in listing}) == (index == 0)
            if index == 0:
                assert json.loads(request(f'/v1/containers/{cid}', opener=opener))['id'] == cid
            else:
                try:
                    request(f'/v1/containers/{cid}', opener=opener)
                except urllib.error.HTTPError as exc:
                    assert exc.code == 404
                else:
                    raise AssertionError('Another member could access the private instance')
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
