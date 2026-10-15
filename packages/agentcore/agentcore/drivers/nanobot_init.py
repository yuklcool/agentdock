"""One-time native initialization, called only while provisioning a new volume."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from agentcore import sandbox
from agentcore.nanobot_files import validate_nanobot_files


def nanobot_workspace(workspace: str) -> Path:
    # The same workspace is enforced by the runtime (not an upstream default).
    return Path(workspace) / "nanobot"


def initialized_config_path(workspace: str) -> Path:
    return Path(workspace) / ".agent-runtime" / "nanobot-init" / "config.json"


async def initialize_nanobot(workspace: str, files: dict[str, str]) -> bool:
    files = validate_nanobot_files(files)
    final = initialized_config_path(workspace)
    if final.is_file():
        return False  # Snapshot semantics: never re-apply, even after an edit.
    native_workspace = nanobot_workspace(workspace)
    if native_workspace.is_symlink() or native_workspace.exists():
        raise ValueError("Nanobot initialization requires a new workspace")
    sandbox.makedirs_agent(str(native_workspace))
    for name, content in files.items():
        if name != "config.json":
            path = native_workspace / name
            path.write_text(content, encoding="utf-8")
            sandbox.chown_to_agent(str(path))
    sandbox.makedirs_agent(sandbox.AGENT_HOME)
    # Native onboard owns generation of config defaults and missing Markdown.
    with tempfile.TemporaryDirectory(prefix="nanobot-init-", dir=sandbox.AGENT_HOME) as temp:
        sandbox.chown_to_agent(temp)
        config = Path(temp) / "config.json"
        if "config.json" in files:
            config.write_text(files["config.json"], encoding="utf-8")
            config.chmod(0o600)
            sandbox.chown_to_agent(str(config))
        proc = await sandbox.spawn_untrusted(
            ["nanobot", "onboard", "--refresh", "--config", str(config),
             "--workspace", str(native_workspace)],
            cwd=workspace, env=sandbox.build_child_env({"HOME": temp}),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(60):
                if await proc.wait() != 0:
                    raise ValueError("Native Nanobot initialization failed")
        finally:
            if proc.returncode is None:
                sandbox.terminate(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except TimeoutError:
                    try:
                        proc.kill()
                    except (PermissionError, ProcessLookupError):
                        pass
                    await asyncio.wait_for(proc.wait(), 3)
        # Validate JSON before marking complete; never persist model credentials.
        native = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(native, dict):
            raise ValueError("Native Nanobot configuration is not an object")
        final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(final.parent, 0o700)
        temporary = final.with_suffix(".tmp")
        temporary.write_text(json.dumps(native, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(final)
    return True
