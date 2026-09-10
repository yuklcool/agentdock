"""AgentDock adapter for the pinned, independently installed Nanobot gateway.

One gateway per container, one connection per task. Turns in a container are
serialized so config/secret rotation cannot restart another active turn.
Secrets live in a temporary runtime directory; native sessions live on the
workspace volume. No Nanobot Python modules are imported by the control plane.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect

from agentcore import sandbox
from agentcore.drivers.base import DriverCapabilities, DriverTemplate, EmitFn, register
from agentcore.drivers.skills_md import valid_skill_name, write_skills
from agentcore.models import (
    AgentConfig,
    ResolvedLimits,
    ShimMcpServer,
    ShimSkill,
    TaskBody,
    TaskResult,
)

_CHAT_ID = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")
# Nanobot derives these directories from the config path, not workspace.
_PERSISTENT_DIRS = ("sessions", "webui", "media", "cron", "logs")


class NanobotError(Exception):
    """A safe error code; never includes credentials or raw subprocess output."""


def runtime_config(
    config: AgentConfig,
    limits: ResolvedLimits,
    credential: str,
    workspace: str,
    token: str,
    servers: list[ShimMcpServer],
    env: dict[str, str],
    base_url: str | None = None,
) -> dict[str, Any]:
    model = config.model
    provider = (
        model.split("/", 1)[0]
        if "/" in model
        else ("anthropic" if model.startswith("claude") else "openai")
    )
    if provider not in {"openai", "anthropic"}:
        raise NanobotError("nanobot_unsupported_provider")
    provider_config: dict[str, Any] = {"apiKey": credential}
    # Keep an explicit instance override compatible with existing deployments.
    if base := env.get("AGENTDOCK_NANOBOT_API_BASE") or base_url:
        provider_config["apiBase"] = base
    if provider == "openai":
        provider_config["apiType"] = "chat_completions"
    mcp: dict[str, Any] = {}
    for server in servers:
        headers = {}
        if server.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {server.secret}"
        elif server.auth_type == "header" and server.auth_header_name:
            headers[server.auth_header_name] = server.secret
        elif server.auth_type != "none":
            raise NanobotError("nanobot_invalid_mcp_auth")
        mcp[server.name] = {"url": server.url, "headers": headers}
    # Only deployment-owned Shim environment may relax native SSRF protection.
    # Never read this policy from the user-editable task/instance `env` mapping.
    try:
        whitelist = [
            str(ipaddress.ip_network(cidr))
            for cidr in os.environ.get("AGENTDOCK_NANOBOT_SSRF_WHITELIST", "").split()
        ]
    except ValueError:
        raise NanobotError("nanobot_invalid_ssrf_whitelist") from None
    return {
        "agents": {
            "defaults": {
                "workspace": str(Path(workspace) / "nanobot"),
                "model": model,
                "provider": provider,
                "maxTokens": min(limits.max_tokens, 8192),
                "maxToolIterations": limits.max_iterations,
                "providerRetryMode": "standard",
                "dream": {"enabled": False},
            }
        },
        "providers": {provider: provider_config},
        "channels": {
            "websocket": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 8765,
                "token": token,
                "websocketRequiresToken": True,
                "allowFrom": ["agentdock-driver"],
                "streaming": True,
            }
        },
        "gateway": {"heartbeat": {"enabled": False}, "restartMode": "exit"},
        "tools": {
            "mcpServers": mcp,
            "restrictToWorkspace": True,
            "ssrfWhitelist": whitelist,
        },
    }


def task_prompt(task: TaskBody, config: AgentConfig) -> str:
    """Carry platform instructions as task context without overwriting AGENTS.md."""
    parts = []
    if config.system_prompt:
        parts.append("AgentDock instructions:\n" + config.system_prompt)
    if config.context.text:
        parts.append("Context:\n" + config.context.text)
    if config.context.variables:
        parts.append("Variables:\n" + json.dumps(config.context.variables, ensure_ascii=False))
    if config.context.files:
        parts.append("Context files:\n" + "\n".join(config.context.files))
    parts.append(task.prompt)
    return "\n\n".join(parts)


class NanobotRuntime:
    def __init__(self, workspace: str, *, runtime_parent: str | None = None) -> None:
        self.workspace = str(Path(workspace).resolve())
        self.runtime_parent = runtime_parent
        self.lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.directory: Path | None = None
        self.token = secrets.token_urlsafe(32)
        self.revision: str | None = None

    def connection(self) -> Any:
        query = urlencode({"client_id": "agentdock-driver", "token": self.token})
        return connect(
            f"ws://127.0.0.1:8765/?{query}",
            proxy=None,
            open_timeout=3,
            close_timeout=2,
            max_size=4 * 1024 * 1024,
        )

    async def ready(self) -> bool:
        if self.process is None or self.process.returncode is not None:
            return False
        try:
            async with self.connection() as ws:
                event = await asyncio.wait_for(ws.recv(), timeout=3)
                return bool(json.loads(event).get("event") == "ready")
        except (OSError, ValueError, TimeoutError):
            return False
        except Exception:
            return False

    async def close(self) -> None:
        proc = self.process
        if proc is not None and proc.returncode is None:
            sandbox.terminate(proc)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), 3)
                except (PermissionError, ProcessLookupError, TimeoutError):
                    raise NanobotError("nanobot_shutdown_failed") from None
        self.process = None
        self.revision = None
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None

    async def ensure_started(
        self,
        native_config: dict[str, Any],
        skills: list[ShimSkill],
        env: dict[str, str],
    ) -> None:
        revision = hashlib.sha256(
            json.dumps(
                {
                    "config": native_config,
                    "skills": [s.model_dump() for s in skills],
                    "env": env,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if self.revision == revision and await self.ready():
            return
        await self.close()
        native_workspace = Path(self.workspace) / "nanobot"
        if native_workspace.is_symlink():
            raise NanobotError("nanobot_workspace_path_conflict")
        sandbox.makedirs_agent(str(native_workspace))
        await self.sync_skills(skills)
        parent = self.runtime_parent or sandbox.AGENT_HOME
        sandbox.makedirs_agent(parent)
        self.directory = Path(tempfile.mkdtemp(prefix="agentdock-nanobot-", dir=parent))
        sandbox.chown_to_agent(str(self.directory))
        persistent = Path(self.workspace) / ".agent-state" / "nanobot" / "data"
        for name in _PERSISTENT_DIRS:
            target = persistent / name
            sandbox.makedirs_agent(str(target), mode=0o700)
            (self.directory / name).symlink_to(target, target_is_directory=True)
        config_path = self.directory / "config.json"
        config_path.write_text(json.dumps(native_config, ensure_ascii=False), encoding="utf-8")
        config_path.chmod(0o600)
        sandbox.chown_to_agent(str(config_path))
        child_env = sandbox.build_child_env(
            {k: v for k, v in env.items() if not k.startswith("AGENTDOCK_NANOBOT_")}
        )
        try:
            self.process = await sandbox.spawn_untrusted(
                ["nanobot", "gateway", "--foreground", "--config", str(config_path)],
                cwd=self.workspace,
                env=child_env,
                # Provider/SDK diagnostics can contain secrets; never persist raw logs.
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(45):
                while not await self.ready():
                    if self.process.returncode is not None:
                        raise NanobotError("nanobot_startup_failed")
                    await asyncio.sleep(0.2)
        except FileNotFoundError:
            await self.close()
            raise NanobotError("nanobot_unavailable") from None
        except TimeoutError:
            await self.close()
            raise NanobotError("nanobot_startup_timeout") from None
        except BaseException:
            await self.close()
            raise
        self.revision = revision

    async def sync_skills(self, skills: list[ShimSkill]) -> None:
        """Replace only platform-managed skills; refuse to overwrite user skills."""
        state = Path(self.workspace) / ".agent-runtime" / "nanobot"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = state / "skills.json"
        previous = set(json.loads(manifest.read_text())) if manifest.exists() else set()
        selected = {s.name for s in skills}
        if len(selected) != len(skills) or any(not valid_skill_name(n) for n in selected):
            raise NanobotError("nanobot_invalid_skill")
        destination = Path(self.workspace) / "nanobot" / "skills"
        if destination.is_symlink():
            raise NanobotError("nanobot_skill_path_conflict")
        for name in previous | selected:
            if not valid_skill_name(name):
                raise NanobotError("nanobot_invalid_skill")
            target = destination / name
            if target.is_symlink() or (target.exists() and name not in previous):
                raise NanobotError("nanobot_skill_path_conflict")
        staging = state / "skills-staging"
        written = await write_skills(str(staging), skills)
        if set(written) != selected:
            shutil.rmtree(staging, ignore_errors=True)
            raise NanobotError("nanobot_skill_sync_failed")
        if previous or selected:
            destination.mkdir(parents=True, exist_ok=True)
            for name in previous:
                shutil.rmtree(destination / name, ignore_errors=True)
            for name in selected:
                shutil.move(str(staging / name), str(destination / name))
        shutil.rmtree(staging, ignore_errors=True)
        manifest.write_text(json.dumps(sorted(selected)), encoding="utf-8")

    async def chat(self, ws: ClientConnection, session_id: str | None, continuation: bool) -> str:
        await receive(ws, "ready")
        # Hash external identifiers before constructing a path (no traversal).
        key = hashlib.sha256(session_id.encode()).hexdigest() if session_id else None
        state = Path(self.workspace) / ".agent-runtime" / "nanobot" / "sessions"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = state / f"{key}.json" if key else None
        if path is not None and path.exists():
            chat_id = json.loads(path.read_text())["chat_id"]
            if not isinstance(chat_id, str) or not _CHAT_ID.fullmatch(chat_id):
                raise NanobotError("nanobot_invalid_session_state")
            await ws.send(json.dumps({"type": "attach", "chat_id": chat_id}))
            attached = await receive(ws, "attached")
            if attached.get("chat_id") != chat_id:
                raise NanobotError("nanobot_session_mismatch")
            return chat_id
        if continuation:
            raise NanobotError("session_state_lost")
        await ws.send(json.dumps({"type": "new_chat"}))
        event = await receive(ws, "attached")
        chat_id = event.get("chat_id")
        if not isinstance(chat_id, str) or not _CHAT_ID.fullmatch(chat_id):
            raise NanobotError("nanobot_invalid_chat_id")
        if path is not None:
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"chat_id": chat_id}), encoding="utf-8")
            temp.replace(path)
        return chat_id


async def receive(ws: ClientConnection, expected: str) -> dict[str, Any]:
    async with asyncio.timeout(10):
        while True:
            frame = json.loads(await ws.recv())
            if not isinstance(frame, dict):
                raise NanobotError("nanobot_invalid_frame")
            if frame.get("event") == "error":
                raise NanobotError("nanobot_protocol_error")
            if frame.get("event") == expected:
                return frame


def _tool_value_text(value: Any) -> str:
    """Normalize Nanobot tool result/error values for AgentDock's text event contract."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _tool_duration_ms(
    event: dict[str, Any], started: dict[str, float], call_id: str
) -> int | None:
    """Prefer a native duration, otherwise measure start -> terminal phase locally."""
    raw = event.get("duration_ms")
    began = started.pop(call_id, None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return max(0, int(raw))
    if began is None:
        return None
    return max(0, int((time.monotonic() - began) * 1000))


async def _emit_tool_events(
    raw_events: Any, emit: EmitFn, started: dict[str, float]
) -> bool:
    """Translate Nanobot WebSocket tool_events into AgentDock's stable event schema."""
    if not isinstance(raw_events, list):
        return False
    emitted = False
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        phase = str(raw.get("phase", "")).lower()
        call_id_raw = raw.get("call_id")
        if not isinstance(call_id_raw, str) or not call_id_raw:
            continue
        call_id = call_id_raw
        if phase == "start":
            started[call_id] = time.monotonic()
            name = raw.get("name")
            await emit(
                "tool_call",
                {
                    "tool_use_id": call_id,
                    "name": name if isinstance(name, str) and name else "tool",
                    "input": raw.get("arguments", {}),
                },
            )
            emitted = True
            continue
        if phase not in {"end", "error"}:
            continue
        value = raw.get("result") if phase == "end" else raw.get("error")
        if value is None:
            extras = {
                key: raw[key]
                for key in ("files", "embeds")
                if raw.get(key) not in (None, [], {})
            }
            value = extras or None
        payload: dict[str, Any] = {
            "tool_use_id": call_id,
            "ok": phase == "end",
            "content": _tool_value_text(value),
        }
        duration_ms = _tool_duration_ms(raw, started, call_id)
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        await emit("tool_result", payload)
        emitted = True
    return emitted


def _file_operation(raw: Any) -> str:
    value = str(raw or "").lower()
    if value in {"add", "added", "create", "created", "new"}:
        return "create"
    if value in {"delete", "deleted", "remove", "removed"}:
        return "delete"
    return "modify"


async def _emit_file_edit(frame: dict[str, Any], emit: EmitFn) -> bool:
    """Normalize Nanobot file_edit frames to the existing file_changed event."""
    phase = str(frame.get("phase", "")).lower()
    if phase in {"start", "starting", "error", "failed"}:
        return False
    raw_edits = frame.get("edits")
    edits = raw_edits if isinstance(raw_edits, list) else [frame]
    emitted = False
    for raw in edits:
        if not isinstance(raw, dict):
            continue
        path = raw.get("path") or frame.get("path")
        if not isinstance(path, str) or not path:
            continue
        payload: dict[str, Any] = {
            "operation": _file_operation(
                raw.get("operation") or raw.get("op") or raw.get("action")
            ),
            "path": path,
        }
        for key in ("diff", "added", "deleted", "old_path", "new_path"):
            if key in raw:
                payload[key] = raw[key]
            elif key in frame:
                payload[key] = frame[key]
        await emit("file_changed", payload)
        emitted = True
    return emitted


class NanobotDriver:
    name = "nanobot"
    capabilities = DriverCapabilities(
        supports_tools=True,
        supports_structured_output=False,
        supports_cancel=True,
        supports_mcp=True,
        supports_skills=True,
    )
    default_template = DriverTemplate(
        driver="nanobot",
        default_system_prompt="",
        available_tools=[],
        tools_user_editable=False,
        supports_context=True,
    )

    def __init__(self) -> None:
        self.runtimes: dict[str, NanobotRuntime] = {}

    async def close(self) -> None:
        for runtime in self.runtimes.values():
            await runtime.close()
        self.runtimes.clear()

    async def run(
        self,
        *,
        task: TaskBody,
        config: AgentConfig,
        limits: ResolvedLimits,
        credential: str,
        emit: EmitFn,
        cancel: asyncio.Event,
        credential_kind: str = "api_key",
        credential_meta: dict[str, Any] | None = None,
        workspace: str = "/workspace",
        skills: list[ShimSkill] | None = None,
        mcp_servers: list[ShimMcpServer] | None = None,
        session_id: str | None = None,
        session_is_continuation: bool = False,
        env: dict[str, str] | None = None,
    ) -> TaskResult:
        await emit("task_started", {"driver": self.name, "model": config.model})
        if credential_kind != "api_key":
            return await self.finish(emit, "nanobot_api_key_required")
        if task.output.type == "structured":
            return await self.finish(emit, "nanobot_structured_output_unsupported")
        if cancel.is_set():
            return await self.finish(emit, "cancelled")
        workspace = str(Path(workspace).resolve())
        runtime = self.runtimes.setdefault(workspace, NanobotRuntime(workspace))

        async def execute() -> str:
            async with runtime.lock:
                native = runtime_config(
                    config,
                    limits,
                    credential,
                    workspace,
                    runtime.token,
                    mcp_servers or [],
                    env or {},
                    base_url=(credential_meta or {}).get("base_url"),
                )
                await runtime.ensure_started(native, skills or [], env or {})
                async with runtime.connection() as ws:
                    chat_id = await runtime.chat(ws, session_id, session_is_continuation)
                    try:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "message",
                                    "chat_id": chat_id,
                                    "content": task_prompt(task, config),
                                }
                            )
                        )
                        return await self.consume(ws, chat_id, emit)
                    except BaseException:
                        # Ensure an abandoned turn cannot keep using tools in the background.
                        try:
                            await self.stop_turn(ws, chat_id)
                        except Exception:
                            await runtime.close()
                        raise

        work = asyncio.create_task(execute())
        cancelled = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {work, cancelled},
                timeout=limits.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work in done:
                return await self.finish(emit, None, work.result())
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return await self.finish(emit, "cancelled" if cancelled in done else "timeout")
        except NanobotError as exc:
            return await self.finish(emit, str(exc))
        except Exception:
            return await self.finish(emit, "nanobot_driver_error")
        finally:
            work.cancel()
            cancelled.cancel()
            await asyncio.gather(work, cancelled, return_exceptions=True)

    @staticmethod
    async def stop_turn(ws: ClientConnection, chat_id: str) -> None:
        await ws.send(json.dumps({"type": "message", "chat_id": chat_id, "content": "/stop"}))
        async with asyncio.timeout(5):
            while True:
                frame = json.loads(await ws.recv())
                if frame.get("chat_id") != chat_id:
                    continue
                if frame.get("event") == "message" and re.fullmatch(
                    r"Stopped \d+ task\(s\)\.|No active task to stop\.",
                    frame.get("text", ""),
                ):
                    return

    @staticmethod
    async def consume(ws: ClientConnection, chat_id: str, emit: EmitFn) -> str:
        streams: dict[str, str] = {}
        final_messages: list[str] = []
        tool_started: dict[str, float] = {}
        while True:
            frame = json.loads(await ws.recv())
            if not isinstance(frame, dict):
                raise NanobotError("nanobot_invalid_frame")
            if frame.get("event") == "error":
                raise NanobotError("nanobot_protocol_error")
            if frame.get("chat_id") != chat_id:
                continue
            event = frame.get("event")
            stream = str(frame.get("stream_id", "default"))
            if event == "delta":
                text = str(frame.get("text", ""))
                streams[stream] = streams.get(stream, "") + text
                await emit("assistant_delta", {"text": text, "stream_id": stream})
            elif event == "stream_end":
                if isinstance(frame.get("text"), str):
                    streams[stream] = frame["text"]
                await emit(
                    "stream_end",
                    {
                        "stream_id": stream,
                        "text": streams.get(stream, ""),
                        "resuming": bool(frame.get("resuming")),
                    },
                )
            elif event in {"reasoning_delta", "reasoning_end"}:
                await emit(str(event), {"text": str(frame.get("text", "")), "stream_id": stream})
            elif event == "message":
                text = str(frame.get("text", ""))
                structured = await _emit_tool_events(frame.get("tool_events"), emit, tool_started)
                if frame.get("kind") in {"progress", "tool_hint"}:
                    # Structured tool events supersede their breadcrumb text. Keep the
                    # legacy log fallback only when no structured event was emitted.
                    if not structured and text.strip():
                        await emit("log", {"level": "info", "message": text})
                elif text:
                    final_messages.append(text)
                    await emit("assistant_message", {"content": [{"type": "text", "text": text}]})
            elif event == "file_edit":
                await _emit_file_edit(frame, emit)
            elif event == "turn_end":
                usage = frame.get("usage")
                if isinstance(usage, dict):
                    await emit(
                        "token_update",
                        {
                            "tokens_in": int(usage.get("prompt_tokens", 0)),
                            "tokens_out": int(usage.get("completion_tokens", 0)),
                        },
                    )
                # A turn can contain multiple streaming segments around tools.
                # Never finish on stream_end or on an intermediate message.
                return (
                    "\n\n".join(final_messages) if final_messages else "\n\n".join(streams.values())
                )

    @staticmethod
    async def finish(emit: EmitFn, reason: str | None, text: str = "") -> TaskResult:
        status = (
            "completed"
            if reason is None
            else (
                "cancelled"
                if reason == "cancelled"
                else "timed_out"
                if reason == "timeout"
                else "failed"
            )
        )
        output = {"success": True, "output": text} if reason is None else None
        await emit(
            "status_change",
            {
                "from": "running",
                "to": status,
                "result": output,
                "error": {"code": reason, "message": reason} if reason else None,
            },
        )
        return TaskResult(success=reason is None, output=output, reason=reason)


register(NanobotDriver())
