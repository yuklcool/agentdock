"""Tenant governance and authenticated Prometheus metrics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.audit import audit
from control_plane.audit_view import enrich_events
from control_plane.auth.principal import Principal, require_session_admin, require_staff
from control_plane.models_db import audit_log, containers, tasks, tenants
from control_plane.routers.containers import _session, _tid, load_tenant_limits

router = APIRouter(tags=["Operations"])


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_private_containers_per_user: int = Field(
        default=1, ge=1, le=1,
        description="Fixed: one bound container per user per workspace.",
    )
    daily_token_budget: int = Field(default=0, ge=0, le=10**12)
    daily_task_limit: int = Field(default=0, ge=0, le=10**9)
    user_daily_token_budget: int = Field(default=0, ge=0, le=10**12)
    user_daily_task_limit: int = Field(default=0, ge=0, le=10**9)


@router.get("/operations/policy", response_model=Policy)
async def get_policy(
    principal: Principal = Depends(require_session_admin),
    session: AsyncSession = Depends(_session),
) -> Policy:
    limits = await load_tenant_limits(session, _tid(principal))
    return Policy(**{key: limits[key] for key in Policy.model_fields})


@router.put("/operations/policy", response_model=Policy)
async def set_policy(
    body: Policy,
    principal: Principal = Depends(require_session_admin),
    session: AsyncSession = Depends(_session),
) -> Policy:
    tid = _tid(principal)
    # JSONB merge avoids losing unrelated settings edited concurrently.
    await session.execute(
        tenants.update()
        .where(tenants.c.id == tid)
        .values(
            limits=sa.cast(tenants.c.limits, sa.dialects.postgresql.JSONB).op("||")(
                sa.cast(body.model_dump(), sa.dialects.postgresql.JSONB)
            ),
        )
    )
    await audit(
        session,
        actor_type="admin",
        actor_id=principal.user_id,
        action="policy.update",
        target_type="tenant",
        target_id=tid,
        details={"tenant_id": tid, **body.model_dump()},
    )
    await session.commit()
    return body


@router.get("/operations/audit")
async def audit_history(
    principal: Principal = Depends(require_session_admin),
    session: AsyncSession = Depends(_session),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    tid = _tid(principal)
    owned = sa.select(containers.c.id).where(containers.c.tenant_id == tid)
    rows = (
        (
            await session.execute(
                sa.select(audit_log)
                .where(
                    sa.or_(
                        audit_log.c.details["tenant_id"].astext == tid,
                        sa.and_(
                            audit_log.c.details["tenant_id"].astext.is_(None),
                            audit_log.c.target_type == "tenant",
                            audit_log.c.target_id == tid,
                        ),
                        sa.and_(
                            audit_log.c.details["tenant_id"].astext.is_(None),
                            audit_log.c.target_type == "container",
                            audit_log.c.target_id.in_(owned),
                        ),
                    )
                )
                .order_by(audit_log.c.ts.desc(), audit_log.c.id.desc())
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    return {"events": await enrich_events(session, tid, list(rows))}


async def _metrics_session(
    request: Request, _: Principal = Depends(require_staff)
) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


@router.get("/operations/metrics", response_class=PlainTextResponse)
async def metrics(
    _: Principal = Depends(require_staff),
    session: AsyncSession = Depends(_metrics_session),
) -> PlainTextResponse:
    # Global metrics require staff. Never include prompts, user ids or credentials.
    lines = ["# TYPE agentdock_up gauge", "agentdock_up 1"]
    for table, name in ((containers, "containers"), (tasks, "tasks")):
        rows = (
            await session.execute(
                sa.select(table.c.status, sa.func.count().label("n")).group_by(table.c.status)
            )
        ).all()
        lines.append(f"# TYPE agentdock_{name} gauge")
        for row in rows:
            # Status is a closed application vocabulary; escape for defense in depth.
            status = row.status.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            lines.append(f'agentdock_{name}{{status="{status}"}} {row.n}')
    stale = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(tasks)
            .where(
                tasks.c.status.in_(("pending", "running")),
                tasks.c.created_at < sa.func.now() - sa.text("interval '1 hour'"),
            )
        )
    ).scalar_one()
    lines.extend(["# TYPE agentdock_stale_tasks gauge", f"agentdock_stale_tasks {stale}"])
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
