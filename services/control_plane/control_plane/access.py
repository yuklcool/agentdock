"""One ownership policy for HTTP, WebSocket, lists and aggregate queries.

NULL ownership is explicitly shared within a tenant (including legacy rows).
User sessions retain owner ACLs; workspace keys have tenant-wide container access.
Background services use unscoped sessions after validating their stored target.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from control_plane.auth.principal import Principal
from control_plane.errors import not_found
from control_plane.models_db import containers


def bind_principal(session: Any, principal: Principal) -> None:
    if not hasattr(session, "info"):
        session.info = {}
    session.info["agentdock_principal"] = principal


def session_principal(session: Any) -> Principal | None:
    info = getattr(session, "info", None)
    return info.get("agentdock_principal") if isinstance(info, dict) else None


def is_manager(principal: Principal) -> bool:
    return principal.is_staff or principal.role in {"owner", "admin"}


def is_workspace_key(principal: Principal) -> bool:
    """A tenant service credential, without user or administrator privileges."""
    return (
        principal.auth_method == "api_key"
        and principal.user_id is None
        and principal.tenant_id is not None
        and not principal.is_staff
    )


def visible_containers(principal: Principal | None) -> Any:
    if principal is None:
        return sa.true()  # internal service session; never used by request dependencies
    tenant = containers.c.tenant_id == principal.tenant_id
    if is_manager(principal) or is_workspace_key(principal):
        return tenant
    ownership = containers.c.owner_user_id.is_(None)
    if principal.user_id is not None:
        ownership = sa.or_(ownership, containers.c.owner_user_id == principal.user_id)
    return sa.and_(tenant, ownership)


def visible_container_ids(session: Any) -> Any:
    return sa.select(containers.c.id).where(visible_containers(session_principal(session)))


def assert_container_access(row: Any, principal: Principal | None) -> None:
    if principal is None:
        return
    owner = getattr(row, "owner_user_id", None)
    if row.tenant_id != principal.tenant_id or (
        owner is not None
        and owner != principal.user_id
        and not is_manager(principal)
        and not is_workspace_key(principal)
    ):
        raise not_found("container not found")


async def assert_target_access(session: Any, container_ids: set[str]) -> None:
    """Validate every container referenced by a workflow/schedule before use."""
    if not container_ids or session_principal(session) is None:
        return
    found = set(
        (
            await session.execute(
                visible_container_ids(session).where(containers.c.id.in_(container_ids))
            )
        ).scalars()
    )
    if found != container_ids:
        raise not_found("target not found")


async def actor_principal(
    session: Any, tenant_id: str, user_id: str | None, role: str = "member"
) -> Principal:
    """Revalidate saved automation identity on each firing/step, including revocation."""
    from control_plane.auth.principal import DbPrincipalRepo
    from control_plane.errors import api_error

    if user_id is None:
        return Principal(
            tenant_id=tenant_id, role="member", is_staff=False, user_id=None, auth_method="api_key"
        )
    repo = DbPrincipalRepo(session)
    user = await repo.get_user(user_id)
    if user and user["status"] == "active":
        if user["is_staff"]:
            return Principal(
                tenant_id=tenant_id,
                role="owner" if role in {"owner", "admin"} else "member",
                is_staff=False,
                user_id=user_id,
            )
        memberships = await repo.get_active_memberships(user_id)
        for membership in memberships:
            if membership["tenant_id"] == tenant_id:
                return Principal(
                    tenant_id=tenant_id,
                    role=membership["role"] if role in {"owner", "admin"} else "member",
                    is_staff=False,
                    user_id=user_id,
                )
    raise api_error(403, "automation_actor_inactive", "Automation owner is no longer authorized")


def visible_owner(column: Any, principal: Principal | None) -> Any:
    if principal is None or is_manager(principal):
        return sa.true()
    if principal.user_id is None:
        return column.is_(None)
    return sa.or_(column.is_(None), column == principal.user_id)


def visible_steps(steps: Any, principal: Principal | None) -> Any:
    """SQL filter for workflow definitions and historical run snapshots."""
    from sqlalchemy.dialects.postgresql import JSONB

    if principal is None or is_manager(principal):
        return sa.true()
    items = (
        sa.func.jsonb_array_elements(sa.func.coalesce(steps, sa.cast(sa.literal("[]"), JSONB)))
        .table_valued(sa.column("value", JSONB))
        .alias("acl_step")
    )
    cid = items.c.value["container_id"].astext
    visible = sa.select(containers.c.id).where(visible_containers(principal))
    return ~sa.exists(
        sa.select(1).select_from(items).where(sa.or_(cid.is_(None), cid.not_in(visible)))
    )
