#!/usr/bin/env python3
"""Generate first-install secrets without requiring a source checkout or pip."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description="生成 AgentDock 首次部署配置")
    parser.add_argument("--email", required=True, help="首次管理员邮箱")
    parser.add_argument("--url", default="http://localhost:8080", help="浏览器访问地址")
    parser.add_argument("--bind", default="127.0.0.1", help="监听 IP；远程访问使用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--version", default="0.3.6")
    parser.add_argument("--registry", default="ghcr.io/yuklcool/agentdock")
    parser.add_argument("--quiet", action="store_true", help="仅生成配置，不输出临时密码")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"https?://[A-Za-z0-9.:[\]-]+", url)
    ):
        parser.error("--url 必须是 HTTP/HTTPS 地址，不包含路径、账号或查询参数")
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", args.email):
        parser.error("邮箱格式无效")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", args.version):
        parser.error("镜像版本格式无效")
    if not re.fullmatch(r"[a-z0-9][a-z0-9./:-]+", args.registry):
        parser.error("镜像仓库格式无效")
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    try:
        ipaddress.IPv4Address(args.bind)
    except ValueError:
        parser.error("--bind 必须为 IPv4 地址")
    password = secrets.token_urlsafe(24)
    values = {
        "AGENTDOCK_REGISTRY": args.registry,
        "AGENTDOCK_VERSION": args.version,
        "AGENTDOCK_BIND": args.bind,
        "AGENTDOCK_PORT": str(args.port),
        "PUBLIC_URL": url,
        "SESSION_COOKIE_SECURE": "true" if parsed.scheme == "https" else "false",
        "DATABASE_URL": "postgresql+asyncpg://agentdock:"
        + secrets.token_hex(24)
        + "@postgres:5432/agentdock",
        "CREDENTIAL_ENCRYPTION_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "CONNECTORS_MASTER_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "ADMIN_API_KEY": "ak_" + secrets.token_urlsafe(32),
        "SEARXNG_SECRET": secrets.token_hex(32),
        "SEED_STAFF_EMAIL": args.email.lower(),
        "SEED_STAFF_PASSWORD": password,
    }
    target = Path(__file__).resolve().parent / ".env"
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error(".env 已存在，未覆盖。升级请保留原密钥并修改 AGENTDOCK_VERSION")
    with os.fdopen(fd, "w") as output:
        output.write("# AgentDock secrets: keep private and preserve for upgrades/backups.\n")
        output.write("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    if not args.quiet:
        print(f"已生成 {target.name}，访问地址：{url}")
        print(f"管理员邮箱：{values['SEED_STAFF_EMAIL']}")
        print(f"临时密码：{password}（首次登录后必须修改）")
        print("接着执行 docker compose --profile images pull 和 docker compose up -d --wait")


if __name__ == "__main__":
    main()
