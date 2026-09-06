"""Idempotent template-to-personal-instance provisioning and personal keys."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

import control_plane.tables as t
from control_plane import lifecycle
from control_plane.access import bind_principal
from control_plane.audit import audit
from control_plane.auth.principal import Principal, resolve_principal
from control_plane.errors import api_error, not_found
from control_plane.models_db import containers, templates
from control_plane.models_db import user_agent_bindings as bindings
from control_plane.routers.api_keys import CreateKey, build_api_key_row, public_view
from control_plane.routers.containers import (
    _load_owned_container,
    _row_to_container_out,
    _session,
    create_container,
    load_tenant_limits,
)
from control_plane.schemas import CreateContainerRequest

router = APIRouter(tags=["Personal agents"])


def require_personal(principal: Principal = Depends(resolve_principal)) -> Principal:
    if principal.tenant_id is None or principal.user_id is None:
        raise api_error(403, "personal_identity_required", "Use a user session or personal API key")
    return principal


def binding_key(tenant_id: str, user_id: str, template_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\0{user_id}\0{template_id}".encode()).hexdigest()
    return "personal:" + digest


@router.post("/templates/{template_id}/my-agent")
async def ensure_my_agent(
    template_id: str,
    request: Request,
    principal: Principal = Depends(require_personal),
    session: AsyncSession = Depends(_session),
) -> dict[str, Any]:
    """Create once per user/template, or restore the existing private instance.

    Database uniqueness and a renewable retry lease work across CP replicas.
    Docker runs outside a transaction. A stable external id recovers a completed
    container when a worker died before recording its binding.
    """
    tid, uid = principal.tenant_id, principal.user_id
    assert tid is not None and uid is not None
    bind_principal(session, principal)
    template = (
        await session.execute(
            sa.select(templates).where(
                templates.c.id == template_id,
                sa.or_(templates.c.tenant_id == tid, templates.c.tenant_id.is_(None)),
            )
        )
    ).first()
    if template is None:
        raise not_found("template not found")
    if template.driver != "nanobot":
        raise api_error(400, "nanobot_template_required", "Choose a Nanobot template")
    if not template.model:
        raise api_error(400, "template_model_required", "Configure a model on this template first")
    key = binding_key(tid, uid, template_id)
    where = sa.and_(
        bindings.c.tenant_id == tid,
        bindings.c.user_id == uid,
        bindings.c.template_id == template_id,
    )
    now = datetime.now(UTC)
    lease = str(uuid.uuid4())
    await session.execute(
        pg_insert(bindings)
        .values(
            tenant_id=tid,
            user_id=uid,
            template_id=template_id,
            lease_id=lease,
            lease_until=now + timedelta(minutes=10),
            status="provisioning",
        )
        .on_conflict_do_nothing()
    )
    binding = (await session.execute(sa.select(bindings).where(where).with_for_update())).first()
    existing = (
        await session.execute(
            sa.select(containers).where(
                containers.c.tenant_id == tid,
                containers.c.external_id == key,
                containers.c.owner_user_id == uid,
                containers.c.template_id == template_id,
                containers.c.status != "destroyed",
            )
        )
    ).first()
    if existing is None and binding.lease_id != lease:
        if binding.status == "provisioning" and binding.lease_until > now:
            await session.rollback()
            raise api_error(409, "instance_provisioning", "Your instance is being created; retry")
        await session.execute(
            sa.update(bindings)
            .where(where)
            .values(
                lease_id=lease,
                lease_until=now + timedelta(minutes=10),
                status="provisioning",
            )
        )
    await session.commit()
    created = False
    try:
        if existing is None:
            request.state.instance_binding_key = key
            out = await create_container(
                request,
                CreateContainerRequest(
                    name=template.name,
                    template_id=template_id,
                    external_id=key,
                    visibility="private",
                ),
                principal,
                session,
            )
            cid = out["id"]
            created = True
        else:
            cid = existing.id
            limits = await load_tenant_limits(session, tid)
            if existing.status in {"paused", "archived"}:
                await lifecycle.restore(
                    session,
                    getattr(request.app.state, "docker_client", None),
                    getattr(request.app.state, "shim", None),
                    cid,
                    tid,
                    limit=int(limits["max_running_containers"]),
                    settings=request.app.state.settings,
                    actor_type="tenant",
                    actor_id=uid,
                )
                await session.commit()
            elif existing.status != "running":
                raise api_error(409, "instance_not_ready", "The instance needs recovery")
            out = _row_to_container_out(await _load_owned_container(session, tid, cid)).model_dump()
        await session.execute(
            sa.update(bindings)
            .where(where)
            .values(
                container_id=cid,
                status="ready",
                updated_at=datetime.now(UTC),
            )
        )
        await audit(
            session,
            actor_type="tenant",
            actor_id=uid,
            action="personal_agent.created" if created else "personal_agent.reused",
            target_type="container",
            target_id=cid,
            details={"tenant_id": tid, "template_id": template_id},
        )
        await session.commit()
        return {"container": out, "created": created}
    except Exception:
        await session.rollback()
        await session.execute(
            sa.update(bindings)
            .where(where, bindings.c.lease_id == lease)
            .values(
                status="error",
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
        raise


def _key_session(principal: Principal) -> None:
    if principal.auth_method != "session":
        raise api_error(403, "session_required", "Manage personal keys from a logged-in session")


@router.get("/me/api-keys")
async def my_keys(
    principal: Principal = Depends(require_personal),
    session: AsyncSession = Depends(_session),
) -> dict[str, Any]:
    _key_session(principal)
    rows = (
        (
            await session.execute(
                sa.select(t.api_keys).where(
                    t.api_keys.c.tenant_id == principal.tenant_id,
                    t.api_keys.c.owner_user_id == principal.user_id,
                )
            )
        )
        .mappings()
        .all()
    )
    return {"keys": [public_view(dict(r)) for r in rows]}


@router.post("/me/api-keys", status_code=201)
async def create_my_key(
    body: CreateKey,
    principal: Principal = Depends(require_personal),
    session: AsyncSession = Depends(_session),
) -> dict[str, Any]:
    _key_session(principal)
    secret, row = build_api_key_row(
        tenant_id=principal.tenant_id,
        name=body.name,
        created_by=principal.user_id,
    )
    row["owner_user_id"] = principal.user_id
    await session.execute(t.api_keys.insert().values(**row))
    await audit(
        session,
        actor_type="tenant",
        actor_id=principal.user_id,
        action="personal_key.created",
        target_type="api_key",
        target_id=row["id"],
        details={"tenant_id": principal.tenant_id},
    )
    await session.commit()
    return {**public_view(row), "key": secret}


@router.delete("/me/api-keys/{key_id}")
async def revoke_my_key(
    key_id: str,
    principal: Principal = Depends(require_personal),
    session: AsyncSession = Depends(_session),
) -> dict[str, str]:
    _key_session(principal)
    result = await session.execute(
        sa.update(t.api_keys)
        .where(
            t.api_keys.c.id == key_id,
            t.api_keys.c.tenant_id == principal.tenant_id,
            t.api_keys.c.owner_user_id == principal.user_id,
        )
        .values(status="revoked", revoked_at=datetime.now(UTC))
        .returning(t.api_keys.c.id)
    )
    if result.scalar_one_or_none() is None:
        raise not_found("key not found")
    await audit(
        session,
        actor_type="tenant",
        actor_id=principal.user_id,
        action="personal_key.revoked",
        target_type="api_key",
        target_id=key_id,
        details={"tenant_id": principal.tenant_id},
    )
    await session.commit()
    return {"id": key_id, "status": "revoked"}
