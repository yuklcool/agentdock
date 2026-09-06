"""Unified Nanobot chat entry point, backed by AgentDock tasks and event replay.

No container addresses or Nanobot tokens leave the control plane. Authorization
uses the same tenant boundary as the REST API (workspace members share access).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, WebSocket
from pydantic import BaseModel, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from agentcore.models import TaskBody
from control_plane.access import bind_principal
from control_plane.auth.principal import Principal
from control_plane.errors import APIError
from control_plane.routers.console import _origin_ok, _principal_ws
from control_plane.routers.containers import _load_owned_container
from control_plane.routers.tasks import (
    _load_owned_task,
    _shim_for,
    submit_task_core,
)
from control_plane.sse import parse_event_line

router = APIRouter(tags=["Nanobot"])


class ChatFrame(BaseModel):
    type: Literal["message", "attach", "cancel", "ping"]
    agent_id: str = Field(default="", max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    content: str = Field(default="", max_length=100 * 1024)
    after_seq: int = Field(default=0, ge=0)


def translate_event(event: dict[str, Any]) -> dict[str, Any]:
    kind = event["type"]
    payload = event.get("payload", {})
    if kind == "assistant_delta":
        return {"event": "delta", **payload}
    if kind in {"reasoning_delta", "reasoning_end", "stream_end"}:
        return {"event": kind, **payload}
    if kind == "assistant_message":
        text = "\n".join(
            str(block.get("text", ""))
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        return {"event": "message", "text": text}
    if kind == "status_change" and payload.get("to") in {
        "completed",
        "failed",
        "cancelled",
        "timed_out",
    }:
        return {
            "event": "turn_end",
            "status": payload["to"],
            "result": payload.get("result"),
            "error": payload.get("error"),
        }
    return {"event": kind, "payload": payload}


@router.websocket("/ws")
async def nanobot_chat(
    websocket: WebSocket,
    principal: Principal | None = Depends(_principal_ws),
) -> None:
    if not _origin_ok(websocket):
        await websocket.close(code=4403)
        return
    if principal is None or principal.tenant_id is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    state = websocket.app.state
    tenant_id = principal.tenant_id
    subscriptions: dict[str, asyncio.Task[None]] = {}
    send_lock = asyncio.Lock()

    async def send(frame: dict[str, Any]) -> None:
        async with send_lock:
            await asyncio.wait_for(websocket.send_json(frame), 10)

    async def forward(crow: Any, task_id: str, session_id: str | None, after: int) -> None:
        shim = _shim_for(state.settings, crow)
        try:
            async for raw in shim.stream_events(task_id, after_seq=after):
                event = parse_event_line(raw.decode("utf-8"))
                if event is None or int(event["seq"]) <= after:
                    continue
                after = int(event["seq"])
                frame = translate_event(event)
                await send(
                    {
                        **frame,
                        "agent_id": crow.id,
                        "session_id": session_id,
                        "task_id": task_id,
                        "seq": after,
                    }
                )
                if frame["event"] == "turn_end":
                    return
        except (WebSocketDisconnect, RuntimeError, TimeoutError):
            pass
        except Exception:
            await send(
                {
                    "event": "error",
                    "code": "event_stream_disconnected",
                    "task_id": task_id,
                    "after_seq": after,
                }
            )
        finally:
            await shim.aclose()

    await send({"event": "ready", "protocol": "agentdock.v1"})
    try:
        while True:
            raw = await asyncio.wait_for(websocket.receive_text(), 900)
            if len(raw.encode("utf-8")) > 110 * 1024:
                await send({"event": "error", "code": "frame_too_large"})
                continue
            try:
                frame = ChatFrame.model_validate_json(raw)
            except ValidationError:
                await send({"event": "error", "code": "invalid_frame"})
                continue
            if frame.type == "ping":
                await send({"event": "pong"})
                continue
            subscriptions = {k: v for k, v in subscriptions.items() if not v.done()}
            try:
                async with state.session_factory() as session:
                    bind_principal(session, principal)
                    crow = await _load_owned_container(session, tenant_id, frame.agent_id)
                    if crow.config.get("driver") != "nanobot":
                        await send({"event": "error", "code": "nanobot_agent_required"})
                        continue
                    if frame.type == "message":
                        if len(subscriptions) >= 8:
                            await send({"event": "error", "code": "too_many_subscriptions"})
                            continue
                        session_id = frame.session_id or str(uuid.uuid4())
                        accepted = await submit_task_core(
                            session,
                            settings=state.settings,
                            session_factory=state.session_factory,
                            docker_client=getattr(state, "docker_client", None),
                            shim_dispatcher=getattr(state, "shim", None),
                            tenant_id=tenant_id,
                            cid=frame.agent_id,
                            body=TaskBody(prompt=frame.content, session_id=session_id),
                        )
                        # Resume can replace the underlying container address.
                        crow = await _load_owned_container(session, tenant_id, frame.agent_id)
                        task_id = accepted["task_id"]
                        after = 0
                    else:
                        if not frame.task_id:
                            await send({"event": "error", "code": "task_id_required"})
                            continue
                        task_id = frame.task_id
                        task = await _load_owned_task(session, tenant_id, frame.agent_id, task_id)
                        session_id = task.session_id
                        if frame.type == "cancel":
                            async with _shim_for(state.settings, crow) as shim:
                                await shim.cancel_task(task_id)
                            await send({"event": "cancel_requested", "task_id": task_id})
                            continue
                        if task_id in subscriptions or len(subscriptions) >= 8:
                            await send({"event": "error", "code": "subscription_conflict"})
                            continue
                        after = frame.after_seq
                # Release the DB connection before opening the streaming subscription.
                await send(
                    {
                        "event": "attached",
                        "agent_id": frame.agent_id,
                        "session_id": session_id,
                        "task_id": task_id,
                    }
                )
                subscriptions[task_id] = asyncio.create_task(
                    forward(crow, task_id, session_id, after)
                )
            except APIError as exc:
                await send({"event": "error", "code": exc.code})
            except Exception:
                await send({"event": "error", "code": "request_failed"})
    except (WebSocketDisconnect, RuntimeError, TimeoutError):
        pass
    finally:
        # Disconnect detaches subscriptions. Tasks keep running and can be reattached.
        for subscription in subscriptions.values():
            subscription.cancel()
        await asyncio.gather(*subscriptions.values(), return_exceptions=True)
