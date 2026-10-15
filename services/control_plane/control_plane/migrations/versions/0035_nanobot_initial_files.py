"""Sparse template overrides and immutable container creation snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0035_nanobot_initial_files"
down_revision = "0034_codex_default_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("templates", "containers"):
        op.add_column(table, sa.Column("nanobot_initial_files", JSONB(), nullable=False,
                                      server_default=sa.text("'{}'::jsonb")))


def downgrade() -> None:
    for table in ("containers", "templates"):
        op.drop_column(table, "nanobot_initial_files")
