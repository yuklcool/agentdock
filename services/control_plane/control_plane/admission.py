"""Atomic per-tenant task admission, with pending-task token reservations.

The PostgreSQL advisory transaction lock covers the check AND task insert.
It is released by the caller's immediate commit, before any network dispatch.
Reported usage replaces reservations when a task becomes terminal. Budgets are
UTC daily admission controls, not a provider-side billing cap.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy import text

from control_plane.errors import api_error, too_many_tasks
from control_plane.models_db import tasks


def check_budget(*, used: int, reserved: int, requested: int, budget: int, code: str) -> None:
    if budget > 0 and used + reserved + requested > budget:
        raise api_error(429, code, "Daily budget exhausted; reduce the task limit or try tomorrow")


async def admit_task(
    session: Any, *, tenant_id: str, user_id: str | None, container_id: str,
    max_tokens: int, worker_cap: int, limits: dict[str, Any],
) -> None:
    await session.execute(sa.text(
        "SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"
    ), {"scope": "agentdock:task-admission:" + tenant_id})
    running = tasks.c.status.in_(("pending", "running"))
    inflight = (await session.execute(sa.select(sa.func.count()).select_from(tasks).where(
        tasks.c.container_id == container_id, running,
    ))).scalar_one()
    if inflight >= worker_cap:
        raise too_many_tasks()
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    reservation = sa.cast(tasks.c.body["limits"]["max_tokens"].astext, sa.BigInteger)
    scopes = [("", None)]
    if user_id is not None:
        scopes.append(("user_", user_id))
    for prefix, actor in scopes:
        token_cap = int(limits.get(prefix + "daily_token_budget", 0))
        task_cap = int(limits.get(prefix + "daily_task_limit", 0))
        if not token_cap and not task_cap:
            continue
        where = [tasks.c.tenant_id == tenant_id]
        if actor is not None:
            where.append(tasks.c.submitted_by == actor)
        row = (await session.execute(sa.select(
            sa.func.coalesce(sa.func.sum(sa.case(
                (sa.and_(~running, tasks.c.ended_at >= start),
                 tasks.c.tokens_in + tasks.c.tokens_out), else_=0,
            )), 0).label("used"),
            sa.func.coalesce(sa.func.sum(sa.case(
                (running, sa.func.coalesce(reservation, int(limits["default_max_tokens"]))),
                else_=0,
            )), 0).label("reserved"),
            sa.func.count().filter(tasks.c.created_at >= start).label("count"),
        ).where(*where))).one()
        check_budget(used=int(row.used), reserved=int(row.reserved), requested=max_tokens,
                     budget=token_cap, code=prefix + "daily_token_budget_exceeded")
        if task_cap > 0 and int(row.count) >= task_cap:
            raise api_error(429, prefix + "daily_task_limit_exceeded", "Daily task limit reached")


# spec §4.4/§4.13: live = running + inbound transients (provisioning, resuming).
# Pausing/archiving/destroying are *leaving* states — not counted toward the live cap.
LIVE_STATES = ("running", "provisioning", "resuming")


# ---- DB protocol (matches AsyncSession.execute signature) ----------------------
class _Executable(Protocol):
    async def execute(
        self, statement: Any, params: Any = None
    ) -> Any: ...  # noqa: E704


# ---- admission queries (spec §4.13) -------------------------------------------

async def live_count(
    db: _Executable,
    tenant_id: str,
    exclude: str | None = None,
) -> int:
    """Number of live containers for *tenant_id*.

    Live means status ∈ {running, provisioning, resuming}.  Pass *exclude* to
    omit one container id from the count (e.g. when evaluating whether to
    create a replacement before destroying the candidate).
    """
    if exclude is None:
        res = await db.execute(
            text(
                "SELECT count(*) FROM containers "
                "WHERE tenant_id = :tid AND status = ANY(:live_states)"
            ),
            {"tid": tenant_id, "live_states": list(LIVE_STATES)},
        )
    else:
        res = await db.execute(
            text(
                "SELECT count(*) FROM containers "
                "WHERE tenant_id = :tid AND status = ANY(:live_states) AND id <> :exclude"
            ),
            {"tid": tenant_id, "live_states": list(LIVE_STATES), "exclude": exclude},
        )
    return int(res.scalar() or 0)


async def active_task_count(db: _Executable, cid: str) -> int:
    """Number of pending-or-running tasks assigned to container *cid*."""
    res = await db.execute(
        text(
            "SELECT count(*) FROM tasks "
            "WHERE container_id = :cid AND status IN ('pending','running')"
        ),
        {"cid": cid},
    )
    return int(res.scalar() or 0)


async def lru_idle_running(db: _Executable, tenant_id: str) -> str | None:
    """Least-recently-used idle live container for *tenant_id*.

    Selects the container with:
    - status = 'running'
    - zero in-flight tasks (no pending/running tasks)
    - oldest last_task_at (NULLS FIRST so never-used containers evict first)

    Returns the container id, or None if all running containers are busy.
    Never returns a busy container (spec §4.13).
    """
    res = await db.execute(
        text(
            "SELECT c.id FROM containers c "
            "WHERE c.tenant_id = :tid AND c.status = 'running' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM tasks t "
            "  WHERE t.container_id = c.id AND t.status IN ('pending','running')"
            ") "
            "ORDER BY c.last_task_at ASC NULLS FIRST "
            "LIMIT 1"
        ),
        {"tid": tenant_id},
    )
    row = res.first()
    return str(row[0]) if row else None
