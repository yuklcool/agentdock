"""One non-destroyed user binding per workspace, with a safe duplicate preflight."""

import sqlalchemy as sa
from alembic import op

revision = "0033_single_user_container"
down_revision = "0032_remove_legacy_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Prevent writes between the duplicate check and index creation.
    op.execute("LOCK TABLE containers IN SHARE ROW EXCLUSIVE MODE")
    conflicts = op.get_bind().execute(sa.text("""
        SELECT tenant_id, owner_user_id, array_agg(id ORDER BY id) AS container_ids
        FROM containers
        WHERE owner_user_id IS NOT NULL AND status <> 'destroyed'
        GROUP BY tenant_id, owner_user_id HAVING count(*) > 1
        ORDER BY tenant_id, owner_user_id LIMIT 20
    """)).mappings().all()
    if conflicts:
        raise RuntimeError(
            "Duplicate user container bindings prevent upgrade (first 20 groups): "
            + repr([dict(row) for row in conflicts])
            + ". No ownership or data was changed. Back up the database and volumes, "
            "then use the previous version to permanently delete unwanted containers "
            "after exporting their data; keep one per workspace/user and retry. "
            "Pausing or archiving does not release a binding. See deploy/OPERATIONS.md."
        )
    op.create_index(
        "uq_container_owner_per_tenant", "containers", ["tenant_id", "owner_user_id"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL AND status <> 'destroyed'"),
    )


def downgrade() -> None:
    op.drop_index("uq_container_owner_per_tenant", table_name="containers")
